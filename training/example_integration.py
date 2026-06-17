#!/usr/bin/env python3
"""
Example: Integrating Trained LoRAs into FluxRT Stream

This script demonstrates how to use trained LoRA adapters in your streaming setup.
Shows both manual style selection and automatic quote-based selection.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Tuple

from training.lora_loader import LoRAManager, FluxLoRA, MusicGenLoRA

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class StreamingStyleManager:
    """Manages style selection and integration for streaming."""

    def __init__(self, lora_dir: str = "training/trained_loras"):
        """Initialize with LoRA manager."""
        self.manager = LoRAManager(lora_dir)
        self.current_flux_style = None
        self.current_musicgen_style = None
        
        logger.info(f"Available Flux styles: {self.manager.list_flux_styles()}")
        logger.info(f"Available MusicGen styles: {self.manager.list_musicgen_styles()}")

    def set_flux_style(self, style_name: str) -> bool:
        """Manually set the current Flux style."""
        if style_name in self.manager.list_flux_styles():
            self.current_flux_style = style_name
            logger.info(f"✓ Flux style set to: {style_name}")
            return True
        else:
            logger.warning(f"✗ Flux style '{style_name}' not found")
            return False

    def set_musicgen_style(self, style_name: str) -> bool:
        """Manually set the current MusicGen style."""
        if style_name in self.manager.list_musicgen_styles():
            self.current_musicgen_style = style_name
            logger.info(f"✓ MusicGen style set to: {style_name}")
            return True
        else:
            logger.warning(f"✗ MusicGen style '{style_name}' not found")
            return False

    def analyze_quote_sentiment(self, quote_text: str) -> Tuple[str, str]:
        """
        Auto-select Flux + MusicGen styles based on quote sentiment.
        
        Returns:
            (flux_style, musicgen_style) tuple
        """
        quote_lower = quote_text.lower()
        
        # Define sentiment keywords
        dark_keywords = ["dark", "death", "sorrow", "pain", "loss", "void", "despair"]
        bright_keywords = ["bright", "joy", "love", "hope", "light", "beauty", "soar"]
        contemplative_keywords = ["think", "wonder", "question", "mystery", "seek", "wisdom"]
        energetic_keywords = ["fast", "rush", "rapid", "dance", "jump", "energy", "move"]
        
        # Score sentiments
        scores = {
            "dark": sum(1 for kw in dark_keywords if kw in quote_lower),
            "bright": sum(1 for kw in bright_keywords if kw in quote_lower),
            "contemplative": sum(1 for kw in contemplative_keywords if kw in quote_lower),
            "energetic": sum(1 for kw in energetic_keywords if kw in quote_lower),
        }
        
        dominant_sentiment = max(scores, key=scores.get)
        
        # Map sentiments to available styles
        style_map = {
            "dark": ("dark_moody", "ambient_dark"),
            "bright": ("vibrant_color", "uplifting_orchestral"),
            "contemplative": ("ethereal_abstract", "contemplative_pad"),
            "energetic": ("vibrant_neon", "energetic_rhythm"),
        }
        
        if dominant_sentiment in style_map:
            flux_style, musicgen_style = style_map[dominant_sentiment]
            
            # Check if styles exist, fall back to available
            if flux_style not in self.manager.list_flux_styles():
                flux_style = self.manager.list_flux_styles()[0] if self.manager.list_flux_styles() else None
            if musicgen_style not in self.manager.list_musicgen_styles():
                musicgen_style = self.manager.list_musicgen_styles()[0] if self.manager.list_musicgen_styles() else None
            
            return flux_style, musicgen_style
        
        # Default fallback
        return (
            self.manager.list_flux_styles()[0] if self.manager.list_flux_styles() else None,
            self.manager.list_musicgen_styles()[0] if self.manager.list_musicgen_styles() else None,
        )

    def get_prompt_with_style(
        self,
        base_prompt: str,
        quote: Optional[str] = None,
        use_auto_style: bool = False
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Build prompt with LoRA trigger phrases.
        
        Args:
            base_prompt: Base prompt text
            quote: Optional quote to analyze for auto-style
            use_auto_style: If True, analyze quote for sentiment-based style
        
        Returns:
            (enhanced_prompt, flux_style, musicgen_style) tuple
        """
        flux_style = self.current_flux_style
        musicgen_style = self.current_musicgen_style
        
        # Auto-select based on quote if requested
        if use_auto_style and quote:
            flux_style, musicgen_style = self.analyze_quote_sentiment(quote)
            self.set_flux_style(flux_style)
            self.set_musicgen_style(musicgen_style)
        
        # Build enhanced prompt
        enhanced_prompt = base_prompt
        
        if flux_style:
            trigger = self.manager.get_trigger_phrase(flux_style, "flux")
            enhanced_prompt += f" {trigger}"
        
        logger.info(f"Enhanced prompt: {enhanced_prompt}")
        logger.info(f"Styles: Flux={flux_style}, MusicGen={musicgen_style}")
        
        return enhanced_prompt, flux_style, musicgen_style


class GradioLoRAIntegration:
    """
    Integration helpers for Gradio app.
    
    Add these to scripts/run_gradio_stream_demo.py:
    """

    @staticmethod
    def create_style_dropdowns(manager: LoRAManager) -> dict:
        """
        Create Gradio dropdown choices for style selection.
        
        Usage in Gradio:
            with gr.Row():
                flux_style_choice = gr.Dropdown(
                    choices=manager.list_flux_styles() + ["None"],
                    value="None",
                    label="Flux Style"
                )
                musicgen_style_choice = gr.Dropdown(
                    choices=manager.list_musicgen_styles() + ["None"],
                    value="None",
                    label="MusicGen Style"
                )
        """
        return {
            "flux_styles": manager.list_flux_styles() + ["None"],
            "musicgen_styles": manager.list_musicgen_styles() + ["None"],
        }

    @staticmethod
    def apply_lora_to_inference(
        pipeline,
        flux_lora: Optional[FluxLoRA],
        prompt: str,
        **generation_kwargs
    ):
        """
        Apply LoRA adapter and generate image.
        
        Usage:
            flux_lora = manager.get_flux_lora(selected_style)
            if flux_lora:
                image = GradioLoRAIntegration.apply_lora_to_inference(
                    pipeline,
                    flux_lora,
                    enhanced_prompt,
                    height=256,
                    width=448,
                    steps=1
                )
        """
        if flux_lora:
            try:
                image = flux_lora.generate(pipeline, prompt, **generation_kwargs)
                logger.info("✓ Generated with LoRA adapter")
                return image
            except Exception as e:
                logger.warning(f"LoRA generation failed: {e}, falling back to base model")
                return None
        return None


# Example usage
if __name__ == "__main__":
    # Initialize
    style_manager = StreamingStyleManager()
    
    # Example 1: Manual style selection
    print("\n=== Example 1: Manual Style Selection ===")
    style_manager.set_flux_style("cyberpunk_neon")
    style_manager.set_musicgen_style("ambient_ethereal")
    
    prompt = "a mystical figure meditating in a digital realm"
    enhanced, flux_style, musicgen_style = style_manager.get_prompt_with_style(prompt)
    print(f"Enhanced prompt: {enhanced}")
    
    # Example 2: Auto-style based on quote
    print("\n=== Example 2: Auto-Style from Quote Sentiment ===")
    quote = "In the depths of winter, I found an invincible summer."
    enhanced, flux_style, musicgen_style = style_manager.get_prompt_with_style(
        prompt,
        quote=quote,
        use_auto_style=True
    )
    print(f"Quote: {quote}")
    print(f"Detected sentiment → Flux: {flux_style}, MusicGen: {musicgen_style}")
    print(f"Enhanced prompt: {enhanced}")
    
    # Example 3: List available styles
    print("\n=== Available Styles ===")
    print(f"Flux: {style_manager.manager.list_flux_styles()}")
    print(f"MusicGen: {style_manager.manager.list_musicgen_styles()}")
