# Custom LoRA Training for FluxRT Streaming

Train custom Flux Klein and MusicGen LoRA adapters to teach your AI stream project-specific styles. LoRAs are parameter-efficient, small (~50-200MB), and can be swapped instantly during live broadcasts.

## Quick Start (5 minutes)

1. **Prepare training data** (10-50 images + captions for Flux)
   ```bash
   mkdir -p training_data/flux_styles/my_style
   # Add images + matching .txt captions
   ```

2. **Train Flux LoRA** (~2 hours on L4)
   ```bash
   uv run training/train_flux_lora.py --style_name my_style --num_epochs 10
   ```

3. **Use in streaming** with trigger phrase `[flux_my_style]`

## Documentation

- **[QUICKREF.md](QUICKREF.md)** ← Start here for 30-second overview
- **[TRAINING.md](TRAINING.md)** ← Full detailed guide with examples
- **[example_integration.py](example_integration.py)** ← Code examples for Gradio app

## What's Included

| File | Purpose |
|------|---------|
| `train_flux_lora.py` | Train image style LoRA (Flux Klein) |
| `train_musicgen_lora.py` | Train music style LoRA (MusicGen) |
| `lora_loader.py` | Integrate LoRAs into inference pipeline |
| `training_config.json` | Hyperparameters and defaults |
| `example_integration.py` | Auto-style detection + Gradio integration |
| `training_data/` | Your datasets (images + audio) |
| `trained_loras/` | Saved LoRA weights after training |

## Key Concepts

### What is LoRA?

**LoRA** = Low-Rank Adaptation
- Train only ~5-10% of model parameters
- Save as small adapter files (~50-200MB)
- Swap styles instantly during streaming
- No base model modification

### Trigger Phrases

After training, LoRAs are accessed via trigger phrases in prompts:

```
Flux:     [flux_style_name]
MusicGen: [musicgen_style_name]
```

Example:
```
"a mystical figure in [flux_cyberpunk] aesthetic, ethereal lighting"
"uplifting orchestral [musicgen_ambient]"
```

### Auto-Style Detection

Automatically select styles based on quote sentiment:
- Dark quotes → dark visual + ambient music style
- Joyful quotes → vibrant visual + uplifting music
- Contemplative quotes → ethereal visual + pad style

## File Structure

```
training/
├── training_data/
│   ├── flux_styles/
│   │   └── my_aesthetic/           ← Put your 10-50 images here
│   │       ├── image_1.jpg
│   │       ├── image_1.txt         ← Caption: "describe the style"
│   │       └── ...
│   └── musicgen_styles/
│       └── my_sound/               ← Put your 5-20 audio clips here
│           ├── sample_1.wav
│           ├── sample_1.txt        ← Description: "describe the music"
│           └── ...
├── trained_loras/
│   ├── flux/
│   │   └── my_aesthetic/           ← Trained weights saved here
│   │       ├── adapter_model/      ← Use this in inference
│   │       └── metadata.json
│   └── musicgen/
│       └── my_sound/               ← Trained weights saved here
│           ├── adapter_model/
│           └── metadata.json
└── *.py files                       ← Training & integration scripts
```

## Workflow

### Phase 1: Data Preparation (30 min)

```bash
# Create training dataset
mkdir -p training_data/flux_styles/cyberpunk_neon
cd training_data/flux_styles/cyberpunk_neon

# Add your images (use symlinks to avoid duplication)
# ln -s /path/to/images/*.jpg .

# Create captions
cat > image_1.txt <<EOF
a person in cyberpunk neon aesthetic, oil painting style, glitch art effect
EOF
# Repeat for each image
```

### Phase 2: Training (2-4 hours)

```bash
cd /home/gschi/FluxRT

# Train Flux LoRA
uv run training/train_flux_lora.py \
    --style_name cyberpunk_neon \
    --data_dir training/training_data/flux_styles/cyberpunk_neon \
    --num_epochs 10 \
    --rank 32

# Optional: Train MusicGen LoRA
uv run training/train_musicgen_lora.py \
    --style_name ambient_dark \
    --data_dir training/training_data/musicgen_styles/ambient_dark \
    --num_epochs 10 \
    --rank 16
```

Weights saved to: `training/trained_loras/flux/cyberpunk_neon/adapter_model/`

### Phase 3: Integration (15 min)

Add to `scripts/run_gradio_stream_demo.py`:

```python
from training.lora_loader import LoRAManager

manager = LoRAManager()
flux_lora = manager.get_flux_lora("cyberpunk_neon")

# In your generate function:
image = flux_lora.generate(
    pipeline,
    prompt="a transmutation in [flux_cyberpunk_neon] aesthetic",
    height=256, width=448, steps=1
)
```

### Phase 4: Live Stream (instant)

Styles appear automatically on your broadcast!

## Common Tasks

### Train Image Style
```bash
uv run training/train_flux_lora.py --style_name my_look --num_epochs 15
```

### Train Music Style
```bash
uv run training/train_musicgen_lora.py --style_name my_sound --num_epochs 10
```

### List Available Styles
```bash
python3 -c "from training.lora_loader import LoRAManager; m = LoRAManager(); print(m.list_flux_styles())"
```

### Test a Trained LoRA
```bash
python3 training/example_integration.py
```

### Delete a Style
```bash
rm -rf training/trained_loras/flux/unwanted_style/
```

## Advanced Features

### Multi-Style Adaptive Prompting

Automatically select different LoRA styles based on quote content:

```python
from training.example_integration import StreamingStyleManager

manager = StreamingStyleManager()
enhanced_prompt, flux_style, musicgen_style = manager.get_prompt_with_style(
    base_prompt="a transmutation",
    quote="In darkness, I found light",
    use_auto_style=True  # ← Auto-detects sentiment
)
```

### Combine Multiple LoRAs

Stack multiple adapters for compound effects:

```python
flux_lora_1 = manager.get_flux_lora("style_1")
flux_lora_2 = manager.get_flux_lora("style_2")

# Generate with both
image = flux_lora_1.generate(pipeline, prompt + " " + manager.get_trigger_phrase("style_2"))
```

## Troubleshooting

**Training runs out of CUDA memory:**
```bash
uv run training/train_flux_lora.py --rank 16 --batch_size 1
```

**Model quality degrading over epochs:**
```bash
uv run training/train_flux_lora.py --num_epochs 5 --learning_rate 5e-5
```

**LoRA not applying to inference:**
1. Verify path: `training/trained_loras/flux/style_name/adapter_model/` exists
2. Check trigger phrase in prompt: `[flux_style_name]`
3. Ensure `lora_loader.py` is in Python path

**For detailed troubleshooting:** See [TRAINING.md](TRAINING.md#troubleshooting)

## Examples

### Example 1: Cyberpunk Style
```bash
# 1. Prepare data: 20 cyberpunk images + "neon glow, glitch, digital, etc." captions
# 2. Train: uv run training/train_flux_lora.py --style_name cyberpunk_neon --num_epochs 15
# 3. Use: "a figure in [flux_cyberpunk_neon] aesthetic"
```

### Example 2: Quote-Responsive Styles
```python
from training.example_integration import StreamingStyleManager

manager = StreamingStyleManager()

quote = "Find strength in your struggles"
enhanced, flux_style, music_style = manager.get_prompt_with_style(
    "a transmutation",
    quote=quote,
    use_auto_style=True
)
# → flux_style="bright", music_style="uplifting_orchestral"
```

### Example 3: Multi-Style Streaming
```python
# Train 3 styles: "dark", "bright", "abstract"
# Gradio dropdown selects style
# Each broadcast uses different aesthetic
# Viewers see consistent project branding
```

## Next Steps

1. **Read [QUICKREF.md](QUICKREF.md)** for 30-second setup
2. **Collect training data** (10-50 images + captions)
3. **Run training script** (~2 hours)
4. **Test with example_integration.py**
5. **Integrate into Gradio app**
6. **Go live** with custom styles!

## Performance Notes

- **Training time:** 2-4 hours per LoRA (L4 GPU)
- **Inference overhead:** ~0 (LoRA weights fused into base model)
- **LoRA file size:** 50-200 MB (easily stored in repo)
- **VRAM required:** Same as base model (no increase)

## Support & Questions

- Full docs: [TRAINING.md](TRAINING.md)
- Code examples: [example_integration.py](example_integration.py)
- Hyperparameters: [training_config.json](training_config.json)
