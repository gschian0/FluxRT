"""
LoRA Loader Module

Integrates trained Flux and MusicGen LoRA adapters into inference.
Used by the streaming pipeline to apply custom styles dynamically.

Example usage:
    from training.lora_loader import FluxLoRA, MusicGenLoRA
    
    # Load Flux style
    flux_lora = FluxLoRA("path/to/trained/flux/cyberpunk")
    
    # Apply in inference
    result = flux_lora.generate(pipe, prompt, width, height, steps)
    
    # Load MusicGen style
    musicgen_lora = MusicGenLoRA("path/to/trained/musicgen/ambient")
    audio = musicgen_lora.generate(pipe, prompt, duration)
"""

import logging
from pathlib import Path
from typing import Optional, Dict, Any

import torch
from peft import AutoPeftModelForCausalLM, PeftModel

logger = logging.getLogger(__name__)


class FluxLoRA:
    """Wrapper for Flux Klein + LoRA adapter."""

    def __init__(self, lora_path: str, device: str = "cuda"):
        """
        Load a trained Flux LoRA adapter.
        
        Args:
            lora_path: Path to trained LoRA weights
            device: Device to load on (cuda, cpu)
        """
        self.lora_path = Path(lora_path)
        self.device = device
        self.adapter_loaded = False
        
        if not self.lora_path.exists():
            raise FileNotFoundError(f"LoRA weights not found: {lora_path}")
        
        logger.info(f"FluxLoRA initialized with: {lora_path}")

    def apply_to_pipeline(self, pipeline) -> None:
        """Apply LoRA adapter to a Flux pipeline."""
        try:
            # Load adapter into transformer
            if hasattr(pipeline.transformer, "load_adapter"):
                pipeline.transformer.load_adapter(
                    str(self.lora_path),
                    adapter_name="project_style"
                )
                pipeline.transformer.set_adapter("project_style")
                logger.info(f"✓ LoRA applied to transformer")
                self.adapter_loaded = True
            else:
                logger.warning("Pipeline transformer doesn't support LoRA loading")
        except Exception as e:
            logger.error(f"Failed to apply LoRA: {e}")
            raise

    def generate(
        self,
        pipeline,
        prompt: str,
        height: int = 256,
        width: int = 448,
        steps: int = 1,
        guidance_scale: float = 3.5,
        **kwargs
    ) -> Any:
        """
        Generate image with LoRA-adapted style.
        
        Args:
            pipeline: Flux pipeline instance
            prompt: Text prompt (can include LoRA trigger phrases)
            height: Image height
            width: Image width
            steps: Inference steps
            guidance_scale: CFG scale
            **kwargs: Additional pipeline args
        
        Returns:
            Generated image
        """
        self.apply_to_pipeline(pipeline)
        
        result = pipeline(
            prompt=prompt,
            height=height,
            width=width,
            num_inference_steps=steps,
            guidance_scale=guidance_scale,
            **kwargs
        )
        
        return result.images[0] if hasattr(result, 'images') else result

    def unload(self) -> None:
        """Disable LoRA adapter."""
        self.adapter_loaded = False
        logger.info("LoRA adapter unloaded")


class MusicGenLoRA:
    """Wrapper for MusicGen + LoRA adapter."""

    def __init__(self, lora_path: str, device: str = "cuda"):
        """
        Load a trained MusicGen LoRA adapter.
        
        Args:
            lora_path: Path to trained LoRA weights
            device: Device to load on (cuda, cpu)
        """
        self.lora_path = Path(lora_path)
        self.device = device
        self.adapter_loaded = False
        
        if not self.lora_path.exists():
            raise FileNotFoundError(f"LoRA weights not found: {lora_path}")
        
        logger.info(f"MusicGenLoRA initialized with: {lora_path}")

    def apply_to_pipeline(self, pipeline) -> None:
        """Apply LoRA adapter to a MusicGen pipeline."""
        try:
            # Load adapter into model
            if hasattr(pipeline.model, "load_adapter"):
                pipeline.model.load_adapter(
                    str(self.lora_path),
                    adapter_name="music_style"
                )
                pipeline.model.set_adapter("music_style")
                logger.info(f"✓ LoRA applied to MusicGen")
                self.adapter_loaded = True
            else:
                logger.warning("MusicGen model doesn't support LoRA loading")
        except Exception as e:
            logger.error(f"Failed to apply LoRA: {e}")
            raise

    def generate(
        self,
        pipeline,
        prompt: str,
        duration: int = 30,
        top_k: int = 250,
        top_p: float = 0.0,
        temperature: float = 1.0,
        **kwargs
    ) -> Any:
        """
        Generate music with LoRA-adapted style.
        
        Args:
            pipeline: MusicGen pipeline instance
            prompt: Text prompt (can include LoRA trigger phrases)
            duration: Duration in seconds (8-30)
            top_k: Top-K sampling
            top_p: Nucleus sampling (disabled if 0)
            temperature: Sampling temperature
            **kwargs: Additional pipeline args
        
        Returns:
            Generated audio
        """
        self.apply_to_pipeline(pipeline)
        
        result = pipeline(
            prompt,
            max_duration=duration,
            top_k=top_k,
            top_p=top_p,
            temperature=temperature,
            **kwargs
        )
        
        return result

    def unload(self) -> None:
        """Disable LoRA adapter."""
        self.adapter_loaded = False
        logger.info("LoRA adapter unloaded")


class LoRAManager:
    """Manages multiple LoRA adapters and style switching."""

    def __init__(self, lora_dir: str = "training/trained_loras"):
        """
        Initialize LoRA manager.
        
        Args:
            lora_dir: Root directory containing flux/ and musicgen/ subdirs
        """
        self.lora_dir = Path(lora_dir)
        self.flux_loras: Dict[str, FluxLoRA] = {}
        self.musicgen_loras: Dict[str, MusicGenLoRA] = {}
        
        self._discover_loras()

    def _discover_loras(self) -> None:
        """Discover all trained LoRA adapters."""
        flux_dir = self.lora_dir / "flux"
        musicgen_dir = self.lora_dir / "musicgen"
        
        # Discover Flux LoRAs
        if flux_dir.exists():
            for style_dir in flux_dir.iterdir():
                if style_dir.is_dir():
                    try:
                        self.flux_loras[style_dir.name] = FluxLoRA(str(style_dir))
                        logger.info(f"Discovered Flux LoRA: {style_dir.name}")
                    except Exception as e:
                        logger.warning(f"Failed to load Flux LoRA {style_dir.name}: {e}")
        
        # Discover MusicGen LoRAs
        if musicgen_dir.exists():
            for style_dir in musicgen_dir.iterdir():
                if style_dir.is_dir():
                    try:
                        self.musicgen_loras[style_dir.name] = MusicGenLoRA(str(style_dir))
                        logger.info(f"Discovered MusicGen LoRA: {style_dir.name}")
                    except Exception as e:
                        logger.warning(f"Failed to load MusicGen LoRA {style_dir.name}: {e}")

    def get_flux_lora(self, style_name: str) -> Optional[FluxLoRA]:
        """Get Flux LoRA by style name."""
        return self.flux_loras.get(style_name)

    def get_musicgen_lora(self, style_name: str) -> Optional[MusicGenLoRA]:
        """Get MusicGen LoRA by style name."""
        return self.musicgen_loras.get(style_name)

    def list_flux_styles(self) -> list:
        """List all available Flux styles."""
        return list(self.flux_loras.keys())

    def list_musicgen_styles(self) -> list:
        """List all available MusicGen styles."""
        return list(self.musicgen_loras.keys())

    def get_trigger_phrase(self, style_name: str, mode: str = "flux") -> str:
        """Get the LoRA trigger phrase for a style."""
        return f"[{mode}_{style_name}]"


if __name__ == "__main__":
    # Quick test
    manager = LoRAManager()
    print("Available Flux styles:", manager.list_flux_styles())
    print("Available MusicGen styles:", manager.list_musicgen_styles())
