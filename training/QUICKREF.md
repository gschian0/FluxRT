# LoRA Training Quick Reference

## File Structure

```
/home/gschi/FluxRT/training/
├── training_data/
│   ├── flux_styles/my_aesthetic/     ← Your image dataset
│   └── musicgen_styles/my_sound/     ← Your audio dataset
├── trained_loras/
│   ├── flux/cyberpunk_neon/          ← Saved Flux LoRA weights
│   └── musicgen/ambient_ethereal/    ← Saved MusicGen LoRA weights
├── train_flux_lora.py                ← Flux training script
├── train_musicgen_lora.py            ← MusicGen training script
├── lora_loader.py                    ← Inference integration
├── example_integration.py            ← Example usage
├── training_config.json              ← Hyperparameters
└── TRAINING.md                       ← Full documentation
```

## 30-Second Setup

### 1. Add Training Data
```bash
# Create Flux dataset
mkdir -p training/training_data/flux_styles/my_look
# Copy 10-50 images to: training/training_data/flux_styles/my_look/*.jpg
# Create caption files: training/training_data/flux_styles/my_look/*.txt

# Create MusicGen dataset (optional)
mkdir -p training/training_data/musicgen_styles/my_sound
# Copy 5-20 audio files: training/training_data/musicgen_styles/my_sound/*.wav
# Create description files: training/training_data/musicgen_styles/my_sound/*.txt
```

### 2. Train Flux LoRA
```bash
cd /home/gschi/FluxRT
uv run training/train_flux_lora.py --style_name my_look --num_epochs 10
```
⏱️ ~2 hours on L4 GPU

### 3. Train MusicGen LoRA (optional)
```bash
cd /home/gschi/FluxRT
uv run training/train_musicgen_lora.py --style_name my_sound --num_epochs 10
```
⏱️ ~1 hour on L4 GPU

## Trigger Phrases

After training, your styles get trigger phrases:

```
Flux:     [flux_my_look]
MusicGen: [musicgen_my_sound]
```

Use in prompts:
```
"a transmutation in [flux_my_look] aesthetic"
"uplifting orchestral [musicgen_my_sound]"
```

## Integration in Gradio App

Add to `scripts/run_gradio_stream_demo.py`:

```python
from training.lora_loader import LoRAManager

manager = LoRAManager()

# In generate function:
flux_lora = manager.get_flux_lora("my_look")
if flux_lora:
    image = flux_lora.generate(
        pipeline,
        prompt="a transmutation in [flux_my_look] aesthetic",
        height=256, width=448, steps=1
    )
```

## Auto-Style Detection

Automatically select styles based on quote sentiment:

```python
from training.example_integration import StreamingStyleManager

manager = StreamingStyleManager()

quote = "In darkness, find the light"
enhanced_prompt, flux_style, musicgen_style = manager.get_prompt_with_style(
    base_prompt="a transmutation",
    quote=quote,
    use_auto_style=True  # ← Auto-selects based on sentiment
)
# Result: flux_style="bright", musicgen_style="uplifting_orchestral"
```

## Parameter Cheat Sheet

### Flux Training
```bash
--style_name        # Required: unique style name (underscore_case)
--num_epochs        # 5-20 (default: 10)
--rank              # 16 (small), 32 (medium), 64 (large) - default: 32
--learning_rate     # 1e-4 (default), 5e-5 (slower), 1e-3 (faster, risky)
--batch_size        # 1 (for L4 GPU)
--data_dir          # Path to images + captions
```

### MusicGen Training
```bash
--style_name        # Required: unique style name
--num_epochs        # 5-15 (default: 10)
--rank              # 8 (small), 16 (medium), 32 (large) - default: 16
--learning_rate     # 1e-4 (default)
--model_id          # musicgen-small (default), musicgen-medium, musicgen-large
```

## Example Caption Files

**Flux (image_1.txt):**
```
a person in oil painting style, cyberpunk neon glitch aesthetic, dark blue and pink lighting, digital surrealism
```

**MusicGen (sample_1.txt):**
```
ambient ethereal pad, slow evolving synthesizer texture, minimalist, spacious reverb, 60 BPM
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Training crashes (CUDA OOM) | Reduce `--rank` to 16 or 8 |
| Model quality degrades | Fewer epochs (5-8) or lower lr (5e-5) |
| LoRA not loading | Check path: `training/trained_loras/*/adapter_model/` exists |
| Trigger phrase not working | Include `[flux_...]` in prompt explicitly |
| No styles discovered | Check folder structure has `adapted_model/` subdirs |

## Monitor Training Progress

Check logs in real-time:
```bash
# Watch loss during training
tail -f training_output.log  # Created during training

# Check checkpoint saved
ls -la training/trained_loras/flux/my_look/
# Should show: adapter_model/, checkpoint_epoch_N/, metadata.json
```

## Next: Integrate into Fanout Stream

Once trained, modify `scripts/run_gradio_stream_demo.py` to apply LoRA:

```python
# At top:
from training.lora_loader import LoRAManager
manager = LoRAManager()

# In inference loop:
selected_style = "my_look"  # Can come from UI dropdown
flux_lora = manager.get_flux_lora(selected_style)

# Generate with style:
if flux_lora:
    image = flux_lora.generate(
        pipeline,
        prompt="transmutation in [flux_my_look]",
        height=256, width=448, steps=1
    )
```

## Full Example

```bash
# 1. Prepare data
mkdir -p training/training_data/flux_styles/steampunk
# Copy 20 steampunk images
# Create matching .txt captions

# 2. Train (2 hours)
uv run training/train_flux_lora.py --style_name steampunk --num_epochs 15 --rank 32

# 3. Verify
ls training/trained_loras/flux/steampunk/adapter_model/

# 4. Test in Gradio
# Modify generate function to use:
# flux_lora = manager.get_flux_lora("steampunk")
# image = flux_lora.generate(pipe, "Victorian airship in [flux_steampunk] aesthetic", ...)

# 5. Live stream
# Users see steampunk aesthetic on broadcast!
```

---

**Questions?** See full docs: `training/TRAINING.md`
