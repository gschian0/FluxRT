#!/usr/bin/env python3
"""Benchmark TensorRT SD-Turbo UNet engine vs baseline PyTorch UNet.

Loads the TensorRT engine, wraps it as a drop-in replacement for the diffusers
UNet, and benchmarks the full img2img pipeline.

Usage:
    CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    /root/fluxrt-venv/bin/python scripts/streaming/benchmark_trt_unet.py
"""
import os
import time
import torch
import numpy as np
import tensorrt as trt
from pathlib import Path
from PIL import Image

ENGINE_PATH = Path("/workspace/FluxRT/models/sd-turbo-unet-trt/engine.plan")

class TRTLogger(trt.ILogger):
    def __init__(self):
        super().__init__()
    def log(self, severity, msg):
        pass  # Suppress TRT logs during inference


class TensorRTUNetWrapper:
    """Drop-in replacement for diffusers UNet that runs inference via TensorRT.
    
    Mimics the UNet2DConditionModel.__call__ interface:
        unet(sample, timestep, encoder_hidden_states) -> noise_pred
    """
    def __init__(self, engine_path, original_unet=None):
        logger = TRTLogger()
        runtime = trt.Runtime(logger)
        with open(engine_path, "rb") as f:
            self.engine = runtime.deserialize_cuda_engine(f.read())
        self.context = self.engine.create_execution_context()
        
        # Get tensor info
        self.input_names = []
        self.output_names = []
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.input_names.append(name)
            else:
                self.output_names.append(name)
        
        # Copy config from original UNet if provided, otherwise use defaults
        if original_unet is not None:
            self._config = original_unet.config
        else:
            self._config = type('Config', (), {
                'in_channels': 4,
                'sample_size': 32,
                'time_cond_proj_dim': None,
                'addition_time_embed_dim': None,
                'projection_class_embeddings_input_dim': 1024,
                'cross_attention_dim': 1024,
                'class_embed_type': None,
                'only_cross_attention': False,
                'num_class_embeds': None,
                'upcast_attention': False,
            })()
        
        print(f"  TRT UNet loaded: {len(self.input_names)} inputs, {len(self.output_names)} outputs")
        for name in self.input_names + self.output_names:
            shape = self.engine.get_tensor_shape(name)
            dtype = self.engine.get_tensor_dtype(name)
            print(f"    {name}: shape={shape}, dtype={dtype}")
    
    def __call__(self, sample, timestep, encoder_hidden_states, **kwargs):
        """Run TensorRT inference, returning output matching diffusers UNet format.
        
        diffusers expects a dict-like return with 'sample' key, or a tuple.
        """
        # Ensure inputs are fp16 on GPU
        sample = sample.half().cuda()
        if timestep.dim() == 0:
            timestep = timestep.unsqueeze(0).half().cuda()
        else:
            timestep = timestep.half().cuda()
        encoder_hidden_states = encoder_hidden_states.half().cuda()
        
        # Allocate output tensor
        out_shape = tuple(self.engine.get_tensor_shape(self.output_names[0]))
        output = torch.empty(out_shape, dtype=torch.float16, device='cuda')
        
        # Set tensor addresses (TRT 11 API)
        self.context.set_input_shape(self.input_names[0], tuple(sample.shape))
        self.context.set_input_shape(self.input_names[1], tuple(timestep.shape))
        self.context.set_input_shape(self.input_names[2], tuple(encoder_hidden_states.shape))
        
        self.context.set_tensor_address(self.input_names[0], sample.data_ptr())
        self.context.set_tensor_address(self.input_names[1], timestep.data_ptr())
        self.context.set_tensor_address(self.input_names[2], encoder_hidden_states.data_ptr())
        self.context.set_tensor_address(self.output_names[0], output.data_ptr())
        
        # Execute
        self.context.execute_async_v3(torch.cuda.current_stream().cuda_stream)
        torch.cuda.synchronize()
        
        # Return in diffusers format (UNet2DConditionOutput-like)
        return output
    
    # Pass through config attribute
    @property
    def config(self):
        return self._config


def benchmark():
    from diffusers import AutoPipelineForImage2Image
    
    print("=== Loading SD-Turbo pipeline (baseline) ===")
    pipe = AutoPipelineForImage2Image.from_pretrained(
        "stabilityai/sd-turbo",
        torch_dtype=torch.float16,
        safety_checker=None,
        requires_safety_checker=False,
    ).to('cuda')
    pipe.vae.enable_slicing()
    
    # Test image
    img = Image.fromarray(np.random.randint(0, 255, (256, 256, 3), dtype=np.uint8))
    
    # Baseline benchmark
    print("\n=== Baseline (PyTorch UNet) ===")
    for _ in range(3):
        _ = pipe(prompt="test", image=img, strength=0.5, num_inference_steps=2, guidance_scale=1.0)
    
    torch.cuda.synchronize()
    t0 = time.time()
    for _ in range(20):
        _ = pipe(prompt="test", image=img, strength=0.5, num_inference_steps=2, guidance_scale=1.0)
    torch.cuda.synchronize()
    baseline_ms = (time.time() - t0) / 20 * 1000
    print(f"  Baseline: {baseline_ms:.0f}ms/frame ({1000/baseline_ms:.1f} fps)")
    
    # Replace UNet with TensorRT
    print("\n=== Loading TensorRT UNet engine ===")
    original_unet = pipe.unet
    trt_unet = TensorRTUNetWrapper(str(ENGINE_PATH), original_unet=original_unet)
    pipe.unet = trt_unet
    
    # Warmup TRT
    print("  Warming up TensorRT...")
    for i in range(5):
        try:
            result = pipe(prompt="test", image=img, strength=0.5, num_inference_steps=2, guidance_scale=1.0)
            print(f"    warmup {i+1} OK")
        except Exception as e:
            print(f"    warmup {i+1} FAILED: {e}")
            import traceback
            traceback.print_exc()
            return
    
    # Benchmark TRT
    torch.cuda.synchronize()
    t0 = time.time()
    for _ in range(20):
        _ = pipe(prompt="test", image=img, strength=0.5, num_inference_steps=2, guidance_scale=1.0)
    torch.cuda.synchronize()
    trt_ms = (time.time() - t0) / 20 * 1000
    print(f"\n  TensorRT: {trt_ms:.0f}ms/frame ({1000/trt_ms:.1f} fps)")
    print(f"  Speedup: {baseline_ms / trt_ms:.2f}x")
    
    # Also benchmark just the UNet forward pass
    print("\n=== Raw UNet forward pass benchmark ===")
    sample = torch.randn(1, 4, 32, 32, dtype=torch.float16).cuda()
    timestep = torch.tensor([999], dtype=torch.float16).cuda()
    encoder_hidden_states = torch.randn(1, 77, 1024, dtype=torch.float16).cuda()
    
    # PyTorch UNet
    from diffusers import UNet2DConditionModel
    pt_unet = UNet2DConditionModel.from_pretrained(
        "stabilityai/sd-turbo", subfolder="unet", torch_dtype=torch.float16
    ).cuda().eval()
    
    for _ in range(3):
        with torch.no_grad():
            _ = pt_unet(sample, timestep, encoder_hidden_states)
    
    torch.cuda.synchronize()
    t0 = time.time()
    for _ in range(50):
        with torch.no_grad():
            _ = pt_unet(sample, timestep, encoder_hidden_states)
    torch.cuda.synchronize()
    pt_unet_ms = (time.time() - t0) / 50 * 1000
    print(f"  PyTorch UNet: {pt_unet_ms:.1f}ms")
    
    # TensorRT UNet
    for _ in range(3):
        _ = trt_unet(sample, timestep, encoder_hidden_states)
    
    torch.cuda.synchronize()
    t0 = time.time()
    for _ in range(50):
        _ = trt_unet(sample, timestep, encoder_hidden_states)
    torch.cuda.synchronize()
    trt_unet_ms = (time.time() - t0) / 50 * 1000
    print(f"  TensorRT UNet: {trt_unet_ms:.1f}ms")
    print(f"  UNet speedup: {pt_unet_ms / trt_unet_ms:.2f}x")
    
    print(f"\n=== Summary ===")
    print(f"  Full pipeline: {baseline_ms:.0f}ms → {trt_ms:.0f}ms ({baseline_ms/trt_ms:.2f}x)")
    print(f"  UNet only:     {pt_unet_ms:.1f}ms → {trt_unet_ms:.1f}ms ({pt_unet_ms/trt_unet_ms:.2f}x)")


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    benchmark()
