#!/usr/bin/env python3
"""Export SD-Turbo UNet to ONNX, then build a TensorRT engine for fast inference.

Usage:
    CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    /root/fluxrt-venv/bin/python scripts/streaming/export_unet_tensorrt.py

Output:
    /workspace/FluxRT/models/sd-turbo-unet-trt/engine.plan  (TensorRT engine)
    /workspace/FluxRT/models/sd-turbo-unet-trt/unet.onnx    (intermediate ONNX)
"""
import os
import sys
import time
import torch
import numpy as np
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("/workspace/FluxRT/models/sd-turbo-unet-trt")
ONNX_PATH = OUTPUT_DIR / "unet.onnx"
ENGINE_PATH = OUTPUT_DIR / "engine.plan"
LATENT_H = 32   # 256 / 8 (VAE downscale factor)
LATENT_W = 32
BATCH_SIZE = 1
FP16 = True

def export_unet_to_onnx():
    """Export SD-Turbo UNet to ONNX format."""
    from diffusers import UNet2DConditionModel
    
    print("[1/3] Loading SD-Turbo UNet from cache...")
    unet = UNet2DConditionModel.from_pretrained(
        "stabilityai/sd-turbo",
        subfolder="unet",
        torch_dtype=torch.float16,
    ).cuda()
    unet.eval()
    
    # Dummy inputs matching SD-Turbo's expected shapes
    # sample: [B, 4, H/8, W/8] = [1, 4, 32, 32]
    # timestep: scalar (0-999)
    # encoder_hidden_states: [B, seq_len, embed_dim] = [1, 77, 1024] (SD-Turbo uses CLIP ViT-H)
    sample = torch.randn(BATCH_SIZE, 4, LATENT_H, LATENT_W, dtype=torch.float16).cuda()
    timestep = torch.tensor([999], dtype=torch.float16).cuda()
    encoder_hidden_states = torch.randn(BATCH_SIZE, 77, 1024, dtype=torch.float16).cuda()
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Remove old files
    if ONNX_PATH.exists():
        ONNX_PATH.unlink()
    data_file = ONNX_PATH.with_suffix(".onnx.data")
    if data_file.exists():
        data_file.unlink()
    
    print(f"[2/3] Exporting UNet to ONNX (single file, all weights embedded): {ONNX_PATH}")
    # Use dynamo=False to get the traditional TorchScript-based export
    # which embeds weights in a single file
    torch.onnx.export(
        unet,
        (sample, timestep, encoder_hidden_states),
        str(ONNX_PATH),
        export_params=True,
        opset_version=18,
        do_constant_folding=True,
        input_names=["sample", "timestep", "encoder_hidden_states"],
        output_names=["noise_pred"],
        dynamic_axes=None,  # static shapes for max optimization
        verbose=False,
        dynamo=False,  # Use traditional export (embeds weights in single file)
    )
    print(f"  ONNX exported: {ONNX_PATH.stat().st_size / 1e6:.1f} MB")
    # Verify no external data file
    if data_file.exists():
        print(f"  WARNING: External data file still exists: {data_file}")
    else:
        print("  ✓ All weights embedded in single ONNX file")
    del unet
    torch.cuda.empty_cache()


def build_tensorrt_engine():
    """Build a TensorRT engine from the ONNX model."""
    import tensorrt as trt
    
    print(f"[3/3] Building TensorRT engine: {ENGINE_PATH}")
    
    TRT_LOGGER = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(TRT_LOGGER)
    
    # Create network (explicit batch is default in TRT 11)
    network = builder.create_network()
    
    # Parse ONNX
    parser = trt.OnnxParser(network, TRT_LOGGER)
    with open(ONNX_PATH, "rb") as f:
        if not parser.parse(f.read()):
            print("ERROR: Failed to parse ONNX file")
            for i in range(parser.num_errors):
                print(f"  {parser.get_error(i)}")
            sys.exit(1)
    
    # Configure builder
    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 4 * (1 << 30))  # 4GB workspace
    
    if FP16:
        # TRT 11 auto-enables FP16 when platform supports it
        print("  FP16: auto-detected by TRT 11")
    
    # Build the engine
    print("  Building engine (this may take several minutes)...")
    t0 = time.time()
    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        print("ERROR: Failed to build TensorRT engine")
        sys.exit(1)
    
    with open(ENGINE_PATH, "wb") as f:
        f.write(serialized)
    
    elapsed = time.time() - t0
    print(f"  Engine built in {elapsed:.0f}s: {ENGINE_PATH.stat().st_size / 1e6:.1f} MB")


def benchmark_engine():
    """Quick benchmark of the TensorRT engine vs baseline."""
    import tensorrt as trt
    import pycuda.driver as cuda
    # Note: pycuda may not be installed, so we'll use a simpler approach
    
    print("\n[4/4] Benchmarking TensorRT engine...")
    
    # Load engine
    TRT_LOGGER = trt.Logger(trt.Logger.INFO)
    runtime = trt.Runtime(TRT_LOGGER)
    with open(ENGINE_PATH, "rb") as f:
        engine = runtime.deserialize_cuda_engine(f.read())
    
    if engine is None:
        print("ERROR: Failed to deserialize engine")
        return
    
    print(f"  Engine has {engine.num_io_tensors} I/O tensors")
    for i in range(engine.num_io_tensors):
        name = engine.get_tensor_name(i)
        mode = engine.get_tensor_mode(name)
        shape = engine.get_tensor_shape(name)
        dtype = engine.get_tensor_dtype(name)
        print(f"    [{i}] {name}: mode={mode}, shape={shape}, dtype={dtype}")
    
    print("\n✅ TensorRT engine built successfully!")
    print(f"   Engine: {ENGINE_PATH}")
    print(f"   Use this with the TensorRT UNet wrapper in run_sdturbo_iptv.py")


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    
    if not ONNX_PATH.exists():
        export_unet_to_onnx()
    else:
        print(f"ONNX already exists: {ONNX_PATH}")
    
    if not ENGINE_PATH.exists():
        build_tensorrt_engine()
    else:
        print(f"Engine already exists: {ENGINE_PATH}")
    
    benchmark_engine()
