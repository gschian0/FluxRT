# Custom LoRA Training for Flux & MusicGen

This guide walks you through training custom LoRA adapters to teach Flux Klein and MusicGen your project-specific styles.

## Overview

**LoRA** (Low-Rank Adaptation) is a parameter-efficient fine-tuning technique that:
- Trains only ~5-10% of parameters (rest frozen)
- Saves as small ~50-200MB adapter files
- Integrates seamlessly into existing pipelines
- Can be swapped out instantly during streaming

## Quick Start

### 1. Prepare Training Data

#### For Flux (Image Style)

```bash
mkdir -p training/training_data/flux_styles/my_aesthetic
cd training/training_data/flux_styles/my_aesthetic

# Add your 10-50 training images:
# image_1.jpg, image_2.jpg, ...
# For each image, create a matching caption file:

echo "a person in cyberpunk neon glitch aesthetic, oil on canvas" > image_1.txt
echo "portrait in surrealist cyberpunk style, digital painting" > image_2.txt
# ... repeat for each image
```

**Caption tips:**
- Describe the visual style, not just the content
- Include medium (oil, digital, watercolor, etc.)
- Include mood/aesthetic (cyberpunk, ethereal, dark, etc.)
- 1-3 sentences per caption

#### For MusicGen (Audio Style)

```bash
mkdir -p training/training_data/musicgen_styles/my_sound
cd training/training_data/musicgen_styles/my_sound

# Add your 5-20 training audio samples:
# sample_1.wav, sample_2.wav, ...
# Create matching descriptions:

echo "ambient ethereal pad, slow evolving texture, minimalist" > sample_1.txt
echo "orchestral swelling strings, cinematic, uplifting energy" > sample_2.txt
# ... repeat for each audio file
```

**Audio tips:**
- Use 10-30 second samples
- Ensure consistent quality
- Describe instruments, mood, tempo
- Include genre/style descriptors

### 2. Install Dependencies

The training environment is already set up, but verify:

```bash
cd /home/gschi/FluxRT
uv sync  # Ensure all dependencies installed
```

### 3. Train Flux LoRA

```bash
cd /home/gschi/FluxRT

uv run training/train_flux_lora.py \
    --style_name cyberpunk_neon \
    --data_dir training/training_data/flux_styles/my_aesthetic \
    --output_dir training/trained_loras/flux \
    --num_epochs 10 \
    --rank 32 \
    --learning_rate 1e-4
```

**Parameters:**
- `--style_name`: Unique name for your style (no spaces, use underscores)
- `--num_epochs`: 5-20 (more = better but slower)
- `--rank`: 32 (good balance) or 64 (more capacity, slower)
- `--learning_rate`: 1e-4 (recommended), 5e-5 for slower learn
- `--batch_size`: 1 (L4 VRAM constraint)

**What to expect:**
- ~1-2 hours for 10 epochs on L4 GPU
- Checkpoints saved after each epoch
- Final weights in `training/trained_loras/flux/cyberpunk_neon/adapter_model/`
- Output will show trigger phrase: `[flux_cyberpunk_neon]`

### 4. Train MusicGen LoRA

```bash
cd /home/gschi/FluxRT

uv run training/train_musicgen_lora.py \
    --style_name ambient_ethereal \
    --data_dir training/training_data/musicgen_styles/my_sound \
    --output_dir training/trained_loras/musicgen \
    --num_epochs 10 \
    --rank 16 \
    --model_id facebook/musicgen-small
```

**Parameters:**
- `--style_name`: Unique music style name
- `--model_id`: `musicgen-small` (recommended), `musicgen-medium`, `musicgen-large`
- `--rank`: 16 (audio) or 32 (more capacity)
- `--num_epochs`: 5-15

**What to expect:**
- ~30-60 minutes for 10 epochs
- Checkpoints saved each epoch
- Final weights in `training/trained_loras/musicgen/ambient_ethereal/adapter_model/`
- Trigger phrase: `[musicgen_ambient_ethereal]`

## Using Trained LoRAs in Streaming

### Automatic Discovery

The LoRA manager automatically discovers all trained adapters:

```python
from training.lora_loader import LoRAManager

manager = LoRAManager()
print(manager.list_flux_styles())       # ['cyberpunk_neon', ...]
print(manager.list_musicgen_styles())   # ['ambient_ethereal', ...]
```

### Manual Integration in Gradio App

Add to `scripts/run_gradio_stream_demo.py`:

```python
from training.lora_loader import LoRAManager

# Initialize manager
lora_manager = LoRAManager()

# In your generate function:
if use_lora:
    style_lora = lora_manager.get_flux_lora("cyberpunk_neon")
    if style_lora:
        image = style_lora.generate(
            pipeline,
            prompt=f"a transmutation in [flux_cyberpunk_neon] aesthetic",
            height=256,
            width=448,
            steps=1
        )
```

### Trigger Phrases in Prompts

When using trained LoRAs, include the trigger phrase in your prompts:

**Flux:**
```
"a mystical figure in [flux_cyberpunk_neon] style, ethereal lighting"
"abstract transmutation with [flux_oil_painting] aesthetic"
```

**MusicGen:**
```
"uplifting orchestral strings with [musicgen_ambient_ethereal] vibe"
"cinematic background score in [musicgen_dark_ambient] mood"
```

## Advanced: Multi-Style Adaptive Prompting

Create a system that automatically selects styles based on quote content:

```python
from training.lora_loader import LoRAManager

manager = LoRAManager()

def select_style_for_quote(quote_text: str) -> str:
    """Auto-select Flux + MusicGen styles based on quote sentiment."""
    
    if any(word in quote_text.lower() for word in ["dark", "death", "sorrow"]):
        return {
            "flux_style": "dark_moody",
            "musicgen_style": "ambient_dark",
            "prompt_suffix": "in [flux_dark_moody] aesthetic, somber mood"
        }
    elif any(word in quote_text.lower() for word in ["bright", "joy", "love"]):
        return {
            "flux_style": "vibrant_color",
            "musicgen_style": "uplifting_orchestral",
            "prompt_suffix": "in [flux_vibrant_color] aesthetic, joyful energy"
        }
    else:
        return {
            "flux_style": "neutral",
            "musicgen_style": "contemplative",
            "prompt_suffix": "in [flux_neutral] aesthetic, balanced composition"
        }

# Usage:
quote = "In the depths of winter, I found an invincible summer."
styles = select_style_for_quote(quote)
```

## Troubleshooting

### Training Runs Out of Memory

```bash
# Reduce batch size (already 1)
# Reduce rank:
--rank 16  # instead of 32

# Enable gradient checkpointing:
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
```

### Poor Quality Results

- **More training data**: Collect 20-50 images/samples instead of 10
- **Better captions**: Be specific about style, mood, medium
- **More epochs**: Try 20-30 instead of 10
- **Higher learning rate**: `1e-3` instead of `1e-4` (may be unstable)

### LoRA Not Loading in Inference

Check that:
1. LoRA path exists: `training/trained_loras/flux/style_name/adapter_model/`
2. File permissions are readable
3. LoRA rank matches training config
4. Trigger phrase is in prompt (not required but recommended)

### Model Getting Worse Over Epochs

- **Overfitting**: Use fewer epochs (5-8)
- **Learning rate too high**: Reduce to `5e-5`
- **Data too small**: Add more training samples

## File Structure

```
training/
├── training_data/
│   ├── flux_styles/
│   │   ├── my_aesthetic/
│   │   │   ├── image_1.jpg
│   │   │   ├── image_1.txt
│   │   │   ├── image_2.jpg
│   │   │   └── image_2.txt
│   │   └── another_style/
│   └── musicgen_styles/
│       ├── ambient_sound/
│       │   ├── sample_1.wav
│       │   ├── sample_1.txt
│       │   └── ...
│       └── orchestral_style/
├── trained_loras/
│   ├── flux/
│   │   ├── cyberpunk_neon/
│   │   │   ├── adapter_model/
│   │   │   ├── metadata.json
│   │   │   └── checkpoint_epoch_10/
│   │   └── oil_painting/
│   └── musicgen/
│       ├── ambient_ethereal/
│       │   ├── adapter_model/
│       │   └── metadata.json
│       └── orchestral_swelling/
├── train_flux_lora.py
├── train_musicgen_lora.py
├── lora_loader.py
└── TRAINING.md (this file)
```

## Next Steps

1. **Collect training data** (10-50 images + captions)
2. **Run Flux LoRA training** (~2 hours)
3. **Collect audio samples** (5-20 music clips + descriptions)
4. **Run MusicGen LoRA training** (~1 hour)
5. **Test in Gradio app** with trigger phrases
6. **Integrate into live stream** with style-switching logic
7. **Monitor broadcast** to see custom styles in action

## Questions?

Check:
- `training/trained_loras/*/metadata.json` for training stats
- Logs printed during training for convergence info
- `lora_loader.py` docstrings for API details
