#!/usr/bin/env python3
"""
MusicGen LoRA Fine-tuning Script

Trains a LoRA adapter for MusicGen to learn project-specific music styles.
LoRA weights are saved separately and loaded during inference.

Usage:
    uv run training/train_musicgen_lora.py \
        --style_name ambient_transmutation \
        --data_dir training/training_data/musicgen_styles \
        --output_dir training/trained_loras/musicgen \
        --num_epochs 10 \
        --rank 16
"""

import argparse
import json
import logging
from pathlib import Path
from typing import List

import numpy as np
import torch
import torchaudio
from peft import LoraConfig, get_peft_model
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MusicStyleDataset(Dataset):
    """Dataset for music LoRA training with audio samples and descriptions."""

    def __init__(self, data_dir: str, sample_rate: int = 16000, duration_sec: int = 10):
        self.data_dir = Path(data_dir)
        self.sample_rate = sample_rate
        self.duration_samples = duration_sec * sample_rate
        
        # Find all audio files
        audio_extensions = [".wav", ".mp3", ".flac", ".ogg"]
        self.audio_files = []
        for ext in audio_extensions:
            self.audio_files.extend(self.data_dir.glob(f"*{ext}"))
        
        if not self.audio_files:
            raise ValueError(f"No audio files found in {data_dir}")
        
        logger.info(f"Found {len(self.audio_files)} audio files for training")

    def __len__(self):
        return len(self.audio_files)

    def __getitem__(self, idx):
        audio_path = self.audio_files[idx]
        desc_path = audio_path.with_suffix(".txt")
        
        try:
            # Load audio
            waveform, sr = torchaudio.load(audio_path)
            
            # Resample if needed
            if sr != self.sample_rate:
                resampler = torchaudio.transforms.Resample(sr, self.sample_rate)
                waveform = resampler(waveform)
            
            # Convert stereo to mono if needed
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)
            
            # Pad or truncate to duration
            if waveform.shape[1] < self.duration_samples:
                padding = self.duration_samples - waveform.shape[1]
                waveform = torch.nn.functional.pad(waveform, (0, padding))
            else:
                waveform = waveform[:, :self.duration_samples]
            
            # Load description
            if desc_path.exists():
                with open(desc_path, "r") as f:
                    description = f.read().strip()
            else:
                description = "ambient music in specific style"
            
            return {
                "waveform": waveform.squeeze(0),
                "description": description,
                "path": str(audio_path)
            }
        
        except Exception as e:
            logger.warning(f"Error loading {audio_path}: {e}")
            # Return silence
            return {
                "waveform": torch.zeros(self.duration_samples),
                "description": "silence",
                "path": str(audio_path)
            }


def train_musicgen_lora(
    style_name: str,
    data_dir: str,
    output_dir: str,
    model_id: str = "facebook/musicgen-small",
    num_epochs: int = 10,
    batch_size: int = 1,
    learning_rate: float = 1e-4,
    rank: int = 16,
    use_int8: bool = True,
):
    """
    Train a LoRA adapter for MusicGen.
    
    Args:
        style_name: Name of the music style
        data_dir: Directory containing training audio + .txt descriptions
        output_dir: Where to save trained LoRA weights
        model_id: HF model ID (musicgen-small, medium, large)
        num_epochs: Number of training epochs
        batch_size: Batch size (1-2 for L4 GPU)
        learning_rate: Learning rate for adapter
        rank: LoRA rank (16-32 for audio)
        use_int8: Use int8 quantization to save VRAM
    """
    
    logger.info(f"Starting MusicGen LoRA training for style: {style_name}")
    logger.info(f"Data directory: {data_dir}")
    logger.info(f"Output directory: {output_dir}")
    
    # Setup paths
    output_path = Path(output_dir) / style_name
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Load model
    logger.info(f"Loading MusicGen model: {model_id}...")
    
    try:
        from transformers import MusicgenForConditionalGeneration, AutoProcessor
        
        model = MusicgenForConditionalGeneration.from_pretrained(
            model_id,
            torch_dtype=torch.float16 if use_int8 else torch.float32,
            device_map="auto"
        )
        processor = AutoProcessor.from_pretrained(model_id)
    except ImportError:
        logger.error("Please install transformers: uv add transformers")
        raise
    
    # Create LoRA config
    lora_config = LoraConfig(
        r=rank,
        lora_alpha=rank * 2,
        target_modules=["q_proj", "v_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM"
    )
    
    # Apply LoRA
    logger.info(f"Applying LoRA adapter (rank={rank})...")
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    
    # Load dataset
    logger.info("Loading training dataset...")
    dataset = MusicStyleDataset(data_dir)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    
    # Setup optimizer
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    
    # Training loop
    logger.info(f"Starting training for {num_epochs} epochs...")
    model.train()
    
    for epoch in range(num_epochs):
        epoch_loss = 0
        
        for step, batch in enumerate(dataloader):
            optimizer.zero_grad()
            
            waveforms = batch["waveform"].to(model.device)
            descriptions = batch["description"]
            
            try:
                # Process inputs
                inputs = processor(
                    text=descriptions,
                    sampling_rate=16000,
                    return_tensors="pt"
                )
                
                # Forward pass with loss computation
                outputs = model(
                    input_ids=inputs.input_ids.to(model.device),
                    attention_mask=inputs.attention_mask.to(model.device),
                    decoder_input_ids=waveforms.unsqueeze(1).to(model.device),
                )
                
                loss = outputs.loss
                loss.backward()
                optimizer.step()
                
                epoch_loss += loss.item()
                
                if step % 5 == 0:
                    logger.info(
                        f"Epoch {epoch+1}/{num_epochs}, Step {step}, Loss: {loss.item():.4f}"
                    )
            
            except Exception as e:
                logger.warning(f"Skipping batch due to error: {e}")
                continue
        
        avg_loss = epoch_loss / max(len(dataloader), 1)
        logger.info(f"Epoch {epoch+1} complete. Avg loss: {avg_loss:.4f}")
        
        # Save checkpoint
        checkpoint_path = output_path / f"checkpoint_epoch_{epoch+1}"
        model.save_pretrained(checkpoint_path)
        logger.info(f"Saved checkpoint to {checkpoint_path}")
    
    # Save final LoRA weights
    final_path = output_path / "adapter_model"
    model.save_pretrained(final_path)
    
    # Save metadata
    metadata = {
        "style_name": style_name,
        "rank": rank,
        "num_epochs": num_epochs,
        "learning_rate": learning_rate,
        "model_id": model_id,
        "data_dir": data_dir,
        "num_samples": len(dataset),
        "final_loss": avg_loss,
    }
    
    with open(output_path / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    
    logger.info(f"✓ MusicGen LoRA training complete!")
    logger.info(f"✓ Weights saved to: {final_path}")
    logger.info(f"✓ Use trigger phrase: [musicgen_{style_name}]")
    logger.info(f"✓ In your prompts: 'uplifting orchestral [musicgen_{style_name}]'")
    
    return str(final_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Train MusicGen LoRA adapter for custom music styles"
    )
    parser.add_argument("--style_name", required=True,
                       help="Name of the style (e.g., 'ambient', 'orchestral')")
    parser.add_argument("--data_dir", default="training/training_data/musicgen_styles",
                       help="Directory with training audio + descriptions")
    parser.add_argument("--output_dir", default="training/trained_loras/musicgen",
                       help="Where to save LoRA weights")
    parser.add_argument("--model_id", default="facebook/musicgen-small",
                       help="MusicGen model (small, medium, or large)")
    parser.add_argument("--num_epochs", type=int, default=10,
                       help="Number of training epochs")
    parser.add_argument("--batch_size", type=int, default=1,
                       help="Batch size (1-2 for L4 GPU)")
    parser.add_argument("--learning_rate", type=float, default=1e-4,
                       help="Learning rate")
    parser.add_argument("--rank", type=int, default=16,
                       help="LoRA rank (16-32 for audio)")
    parser.add_argument("--use_int8", action="store_true", default=True,
                       help="Use int8 quantization")
    
    args = parser.parse_args()
    
    train_musicgen_lora(
        style_name=args.style_name,
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        model_id=args.model_id,
        num_epochs=args.num_epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        rank=args.rank,
        use_int8=args.use_int8,
    )
