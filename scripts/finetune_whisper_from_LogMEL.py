#!/usr/bin/env python3

#############################################################################
# Script Name: finetune_whisper_from_LogMEL.py                             #
# Description: Fine-tune Whisper from pre-processed log-Mel spectrograms   #
# Author: Hanno Müller                                                      #
# Date: 2025-09-01                                                          #
# Based on: finetune_whisper.py (modified to skip preprocessing)           #
#                                                                           #
# Key differences from original:                                            #
# - Skips audio preprocessing step                                          #
# - Expects input dataset to already have 'input_features' and 'labels'    #
# - Validates dataset structure before training                             #
# - Same training logic and WER computation with text normalization        #
#############################################################################

import os
import torch
import evaluate
import argparse
import numpy as np
import string
from dataclasses import dataclass
from typing import Any, Dict, List, Union

from datasets import load_from_disk, DatasetDict
from transformers import (
    WhisperFeatureExtractor,
    WhisperTokenizer,
    WhisperProcessor,
    WhisperForConditionalGeneration,
)
from transformers.training_args_seq2seq import Seq2SeqTrainingArguments
from transformers.trainer_seq2seq import Seq2SeqTrainer

# ============================================================================
# Argument Parsing
# ============================================================================

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Fine-tune Whisper model from pre-processed log-Mel spectrograms with improved stability measures."
    )
    
    parser.add_argument(
        "--dataset_path",
        type=str,
        required=True,
        help="Path to the pre-processed dataset folder (e.g., data/LangAgeLogMelSpec)"
    )
    
    parser.add_argument(
        "--model_size",
        type=str,
        default="medium",
        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
        help="Whisper model size (default: medium)"
    )
    
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./FrisperWhisper/test-gpu",
        help="Output directory for the fine-tuned model (default: ./FrisperWhisper/test-gpu)"
    )
    
    # Core training hyperparameters (improved defaults)
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=4,  # Reduced for stability
        help="Batch size per device during training (default: 4)"
    )
    
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=4,  # Increased for better effective batch size
        help="Number of updates steps to accumulate before performing a backward/update pass (default: 4)"
    )
    
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=3e-6,  # Even more conservative for stability
        help="Learning rate (default: 3e-6)"
    )
    
    parser.add_argument(
        "--warmup_steps",
        type=int,
        default=1000,  # Extended warmup for stability
        help="Number of warmup steps (default: 1000)"
    )
    
    parser.add_argument(
        "--max_steps",
        type=int,
        default=40000,  # More training for better convergence
        help="Maximum number of training steps (default: 40000)"
    )
    
    parser.add_argument(
        "--save_steps",
        type=int,
        default=1000,  # More frequent saves
        help="Save checkpoint every X steps (default: 1000)"
    )
    
    parser.add_argument(
        "--eval_steps",
        type=int,
        default=1000,  # More frequent evaluation
        help="Evaluate every X steps (default: 1000)"
    )
    
    # Stability measures
    parser.add_argument(
        "--max_grad_norm",
        type=float,
        default=0.5,  # More aggressive clipping for stability
        help="Maximum gradient norm for clipping (default: 0.5, set to 0 to disable)"
    )
    
    parser.add_argument(
        "--weight_decay",
        type=float,
        default=0.02,  # Slightly higher regularization
        help="Weight decay (L2 regularization) coefficient (default: 0.02)"
    )
    
    parser.add_argument(
        "--lr_scheduler_type",
        type=str,
        default="cosine",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
        help="Learning rate scheduler type (default: cosine)"
    )
    
    parser.add_argument(
        "--early_stopping_patience",
        type=int,
        default=0,
        help="Early stopping patience (steps). Set to 0 to disable early stopping (default: 0)"
    )
    
    parser.add_argument(
        "--early_stopping_threshold",
        type=float,
        default=0.01,
        help="Early stopping threshold for metric improvement (default: 0.01)"
    )
    
    # Advanced training options
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=8,
        help="Number of data loader workers (default: 8)"
    )
    
    parser.add_argument(
        "--fp16",
        action="store_true",
        default=False,  # Disable by default since BF16 is preferred
        help="Use 16-bit floating point precision (default: False, use BF16 instead)"
    )
    
    parser.add_argument(
        "--bf16",
        action="store_true",
        default=True,  # Enable by default for H100
        help="Use bfloat16 precision (recommended for H100, default: True)"
    )
    
    parser.add_argument(
        "--num_gpus",
        type=int,
        default=0,
        help="Number of GPUs to use (default: 0 for CPU-only training)"
    )
    
    # Logging and monitoring
    parser.add_argument(
        "--logging_steps",
        type=int,
        default=50,
        help="Log training metrics every X steps (default: 50)"
    )
    
    parser.add_argument(
        "--report_to",
        type=str,
        nargs="+",
        default=["tensorboard"],
        choices=["tensorboard", "wandb", "none"],
        help="Logging platforms (default: tensorboard)"
    )
    
    # Checkpoint resumption
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="Path to a checkpoint to resume training from. Can be a specific checkpoint directory or 'True' to auto-resume from the latest checkpoint in output_dir (default: None)"
    )
    
    return parser.parse_args()

# ============================================================================
# Text Normalization for WER Computation
# ============================================================================

def normalize_text_for_wer(text):
    """
    Normalize text for accurate WER computation by handling differences
    between original transcripts and Whisper output.
    
    Simple normalization:
    - Convert all letters to lowercase
    - Remove final punctuation
    - Remove all commas
    """
    if not text:
        return ""
    
    # Convert to lowercase
    text = text.lower()
    
    # Remove all commas
    text = text.replace(',', '')
    
    # Remove punctuation at the end of sentences
    text = text.rstrip(string.punctuation + ' ')
    
    return text

# ============================================================================
# Step 1: Load and Validate Pre-processed Dataset
# ============================================================================

def get_expected_mel_bins(model_name):
    """Get expected number of mel bins for a given Whisper model."""
    if "large-v3" in model_name:
        return 128
    else:
        return 80

def load_and_validate_dataset(dataset_path, model_name):
    """Load and validate pre-processed dataset from disk."""
    print(f"Loading pre-processed dataset from: {dataset_path}")
    
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")
    
    dataset = load_from_disk(dataset_path)
    
    print(f"Dataset loaded successfully!")
    print(f"Available splits: {list(dataset.keys())}")
    
    # Get expected mel bins for this model
    expected_mel_bins = get_expected_mel_bins(model_name)
    print(f"Expected mel bins for {model_name}: {expected_mel_bins}")
    
    # Validate that the dataset has the required columns
    required_columns = ["input_features", "labels"]
    
    for split_name, split_data in dataset.items():
        print(f"\n{split_name.capitalize()} split:")
        print(f"  Size: {len(split_data)} samples")
        print(f"  Columns: {split_data.column_names}")
        
        # Check for required columns
        missing_columns = [col for col in required_columns if col not in split_data.column_names]
        if missing_columns:
            raise ValueError(f"Missing required columns in {split_name} split: {missing_columns}")
        
        # Validate sample data structure
        sample = split_data[0]
        
        # Check input_features structure
        input_features = sample["input_features"]
        if isinstance(input_features, list):
            input_features_shape = (len(input_features), len(input_features[0]) if input_features else 0)
        else:
            input_features_shape = input_features.shape if hasattr(input_features, 'shape') else "unknown"
        
        print(f"  Sample input_features shape: {input_features_shape}")
        print(f"  Sample labels length: {len(sample['labels'])}")
        
        # Additional validation for Whisper format
        if isinstance(input_features, list) and len(input_features) > 0:
            # For list format, mel bins are the first dimension
            actual_mel_bins = len(input_features) if isinstance(input_features[0], list) else "unknown"
        elif hasattr(input_features, 'shape') and len(input_features.shape) >= 2:
            # For numpy/tensor format, mel bins are the first dimension
            actual_mel_bins = input_features.shape[0]
        else:
            actual_mel_bins = "unknown"
            
        if isinstance(actual_mel_bins, int):
            if actual_mel_bins == expected_mel_bins:
                print(f"  ✓ Mel bins: {actual_mel_bins} (matches {model_name} requirements)")
            elif actual_mel_bins in [80, 128]:
                print(f"  ⚠ Mel bins: {actual_mel_bins} (expected {expected_mel_bins} for {model_name})")
                print(f"    This dataset may have been preprocessed for a different Whisper model version.")
                print(f"    - 80 mel bins: for tiny/base/small/medium/large/large-v2")
                print(f"    - 128 mel bins: for large-v3")
            else:
                print(f"  ❌ Mel bins: {actual_mel_bins} (invalid for Whisper models)")
                raise ValueError(f"Invalid mel bins count: {actual_mel_bins}. Expected 80 or 128.")
        else:
            print(f"  Could not determine mel bins: {actual_mel_bins}")
    
    print(f"\nDataset validation completed successfully!")
    return dataset

# ============================================================================
# Step 2: Load Whisper Components (for tokenizer and model compatibility)
# ============================================================================

def load_tokenizer(model_name):
    """Load WhisperTokenizer."""
    print(f"Loading tokenizer for {model_name}...")
    tokenizer = WhisperTokenizer.from_pretrained(
        model_name, 
        language="French", 
        task="transcribe"
    )
    return tokenizer

def create_processor(model_name):
    """Create WhisperProcessor."""
    print(f"Creating processor for {model_name}...")
    processor = WhisperProcessor.from_pretrained(
        model_name, 
        language="French", 
        task="transcribe"
    )
    
    return processor

# ============================================================================
# Step 3: Load Pre-trained Model
# ============================================================================

def load_pretrained_model(model_name):
    """Load pre-trained Whisper model."""
    print(f"Loading pre-trained model: {model_name}")
    
    # Load model normally - should not have missing keys for standard Whisper models
    model = WhisperForConditionalGeneration.from_pretrained(model_name)
    
    # Disable cache to avoid issues with DataParallel and Whisper large-v3
    model.config.use_cache = False
    
    # Create a clean generation config to fix warning 3
    # Remove generation parameters from model config and set them in generation config
    generation_config_params = {}
    if hasattr(model.config, 'max_length'):
        generation_config_params['max_length'] = model.config.max_length
        delattr(model.config, 'max_length')
    if hasattr(model.config, 'suppress_tokens'):
        generation_config_params['suppress_tokens'] = model.config.suppress_tokens
        delattr(model.config, 'suppress_tokens')
    if hasattr(model.config, 'begin_suppress_tokens'):
        generation_config_params['begin_suppress_tokens'] = model.config.begin_suppress_tokens
        delattr(model.config, 'begin_suppress_tokens')
    if hasattr(model.config, 'forced_decoder_ids'):
        # Remove deprecated forced_decoder_ids
        delattr(model.config, 'forced_decoder_ids')
    
    # Update generation config with the parameters
    for key, value in generation_config_params.items():
        setattr(model.generation_config, key, value)
    
    return model

# ============================================================================
# Step 4: Define Data Collator
# ============================================================================

@dataclass
class DataCollatorSpeechSeq2SeqWithPadding:
    processor: Any
    decoder_start_token_id: int

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        # Split inputs and labels since they have to be of different lengths and need different padding methods
        # First treat the audio inputs by simply returning torch tensors
        input_features = [{"input_features": feature["input_features"]} for feature in features]
        batch = self.processor.feature_extractor.pad(input_features, return_tensors="pt")

        # Get the tokenized label sequences
        label_features = [{"input_ids": feature["labels"]} for feature in features]
        # Pad the labels to max length
        labels_batch = self.processor.tokenizer.pad(label_features, return_tensors="pt")

        # Replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(labels_batch.attention_mask.ne(1), -100)

        # If bos token is appended in previous tokenization step,
        # cut bos token here as it's append later anyways
        if (labels[:, 0] == self.decoder_start_token_id).all().cpu().item():
            labels = labels[:, 1:]

        batch["labels"] = labels

        return batch

# ============================================================================
# Step 5: Evaluation Metrics
# ============================================================================

def load_metric():
    """Load WER metric."""
    print("Loading WER metric...")
    metric = evaluate.load("wer")
    return metric

def create_compute_metrics_function(tokenizer, metric):
    """Create compute_metrics function with proper text normalization."""
    def compute_metrics(pred):
        pred_ids = pred.predictions
        label_ids = pred.label_ids

        # Replace -100 with the pad_token_id
        label_ids[label_ids == -100] = tokenizer.pad_token_id

        # We do not want to group tokens when computing the metrics
        pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
        label_str = tokenizer.batch_decode(label_ids, skip_special_tokens=True)

        # Apply text normalization to both predictions and references
        # This handles differences between original transcripts (lowercase, no punctuation)
        # and Whisper output (capitalized, with punctuation)
        pred_str_normalized = [normalize_text_for_wer(text) for text in pred_str]
        label_str_normalized = [normalize_text_for_wer(text) for text in label_str]

        wer = 100 * metric.compute(predictions=pred_str_normalized, references=label_str_normalized)

        return {"wer": wer}
    
    return compute_metrics

# ============================================================================
# Step 6: Training Arguments and Trainer
# ============================================================================

def create_training_arguments(args):
    """Create training arguments based on command line arguments."""
    # Set pin_memory based on GPU availability
    use_pin_memory = args.num_gpus > 0
    
    # Automatically disable BF16/FP16 for CPU training
    use_bf16 = args.bf16 and args.num_gpus > 0
    use_fp16 = args.fp16 and args.num_gpus > 0 and not use_bf16
    
    if args.num_gpus == 0:
        if args.bf16 or args.fp16:
            print("Note: Disabling BF16/FP16 for CPU training")
    
    # Handle early stopping
    early_stopping_kwargs = {}
    if args.early_stopping_patience > 0:
        early_stopping_kwargs.update({
            "load_best_model_at_end": True,
            "metric_for_best_model": "wer",
            "greater_is_better": False,
        })
    
    training_args = Seq2SeqTrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        warmup_steps=args.warmup_steps,
        max_steps=args.max_steps,
        
        # Stability measures
        max_grad_norm=args.max_grad_norm if args.max_grad_norm > 0 else 1.0,  # Use 1.0 as default instead of None
        weight_decay=args.weight_decay,
        lr_scheduler_type=args.lr_scheduler_type,
        
        # Memory and performance - automatically disable for CPU
        fp16=use_fp16,
        bf16=use_bf16,
        
        # Evaluation and logging
        eval_strategy="steps",
        per_device_eval_batch_size=2,  # Keep small for memory efficiency
        predict_with_generate=True,
        generation_max_length=225,
        save_steps=args.save_steps,
        eval_steps=args.eval_steps,
        logging_steps=args.logging_steps,
        report_to=args.report_to,
        
        # Data loading
        dataloader_pin_memory=use_pin_memory,
        dataloader_num_workers=args.dataloader_num_workers,
        remove_unused_columns=False,  # Keep columns to avoid issues
        group_by_length=False,  # Disable to avoid DataParallel issues
        
        # Multi-GPU optimizations
        ddp_find_unused_parameters=False,  # For better performance
        
        # Apply early stopping settings
        **early_stopping_kwargs,
    )
    
    return training_args

def create_trainer(training_args, model, train_dataset, eval_dataset, data_collator, compute_metrics, processor, args):
    """Create Seq2SeqTrainer with optional early stopping."""
    from transformers.trainer_callback import EarlyStoppingCallback
    
    # Setup callbacks
    callbacks = []
    if args.early_stopping_patience > 0:
        early_stopping_callback = EarlyStoppingCallback(
            early_stopping_patience=args.early_stopping_patience,
            early_stopping_threshold=args.early_stopping_threshold
        )
        callbacks.append(early_stopping_callback)
        print(f"Early stopping enabled: patience={args.early_stopping_patience}, threshold={args.early_stopping_threshold}")
    
    trainer = Seq2SeqTrainer(
        args=training_args,
        model=model,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=data_collator,
        compute_metrics=compute_metrics,
        processing_class=processor,  # Use processing_class instead of tokenizer
        callbacks=callbacks,
    )
    return trainer

# ============================================================================
# Checkpoint Management
# ============================================================================

def find_latest_checkpoint(output_dir):
    """Find the latest checkpoint in the output directory."""
    import glob
    import re
    
    if not os.path.exists(output_dir):
        return None
    
    # Look for checkpoint directories
    checkpoint_pattern = os.path.join(output_dir, "checkpoint-*")
    checkpoints = glob.glob(checkpoint_pattern)
    
    if not checkpoints:
        return None
    
    # Extract step numbers and find the latest
    checkpoint_steps = []
    for checkpoint in checkpoints:
        match = re.search(r"checkpoint-(\d+)", checkpoint)
        if match:
            step = int(match.group(1))
            checkpoint_steps.append((step, checkpoint))
    
    if not checkpoint_steps:
        return None
    
    # Return the checkpoint with the highest step number
    latest_checkpoint = max(checkpoint_steps, key=lambda x: x[0])[1]
    return latest_checkpoint

def resolve_checkpoint_path(resume_from_checkpoint, output_dir):
    """Resolve the checkpoint path for resumption."""
    if resume_from_checkpoint is None:
        return None
    
    # Handle different cases
    if resume_from_checkpoint.lower() == "true":
        # Auto-detect latest checkpoint in output_dir
        checkpoint_path = find_latest_checkpoint(output_dir)
        if checkpoint_path:
            print(f"Auto-detected latest checkpoint: {checkpoint_path}")
        else:
            print(f"No checkpoints found in {output_dir}, starting fresh training")
        return checkpoint_path
    
    elif os.path.isabs(resume_from_checkpoint):
        # Absolute path provided
        if os.path.exists(resume_from_checkpoint):
            print(f"Resuming from checkpoint: {resume_from_checkpoint}")
            return resume_from_checkpoint
        else:
            print(f"Warning: Checkpoint path does not exist: {resume_from_checkpoint}")
            print("Starting fresh training")
            return None
    
    else:
        # Relative path - assume it's relative to output_dir
        checkpoint_path = os.path.join(output_dir, resume_from_checkpoint)
        if os.path.exists(checkpoint_path):
            print(f"Resuming from checkpoint: {checkpoint_path}")
            return checkpoint_path
        else:
            print(f"Warning: Checkpoint path does not exist: {checkpoint_path}")
            print("Starting fresh training")
            return None

# ============================================================================
# Step 7: Training Function
# ============================================================================

def train_model(trainer, resume_from_checkpoint=None):
    """Train the model with optional checkpoint resumption."""
    if resume_from_checkpoint:
        print(f"Resuming training from checkpoint: {resume_from_checkpoint}")
    else:
        print("Starting training from scratch...")
    
    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
    print("Training completed!")

# ============================================================================
# Configuration Summary and README Generation
# ============================================================================

def create_training_readme(args):
    """Create a README.md file in the output directory with training configuration."""
    import os
    
    # Ensure output directory exists
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Extract model name from output directory for title
    model_name = os.path.basename(args.output_dir)
    
    # Create README content
    readme_content = f"""# Whisper Large-v3 Fine-tuning - {model_name}

## Training Configuration

```bash
python scripts/finetune_whisper_from_LogMEL.py \\
    --dataset_path "{args.dataset_path}" \\
    --output_dir "{args.output_dir}" \\
    --model_size "{args.model_size}" \\
    --num_gpus {args.num_gpus} \\
    --dataloader_num_workers {args.dataloader_num_workers} \\
    --per_device_train_batch_size {args.per_device_train_batch_size} \\
    --gradient_accumulation_steps {args.gradient_accumulation_steps} \\
    --learning_rate {args.learning_rate} \\
    --max_steps {args.max_steps} \\
    --warmup_steps {args.warmup_steps} \\
    --save_steps {args.save_steps} \\
    --eval_steps {args.eval_steps} \\
    --logging_steps {args.logging_steps} \\
    --max_grad_norm {args.max_grad_norm} \\
    --weight_decay {args.weight_decay} \\
    --lr_scheduler_type "{args.lr_scheduler_type}" \\"""
    
    # Add optional arguments that might be present
    if hasattr(args, 'bf16') and args.bf16:
        readme_content += "\n    --bf16 \\"
    if hasattr(args, 'fp16') and args.fp16:
        readme_content += "\n    --fp16 \\"
    if hasattr(args, 'report_to') and args.report_to:
        readme_content += f'\n    --report_to "{args.report_to}" \\'
    if hasattr(args, 'early_stopping_patience') and args.early_stopping_patience > 0:
        readme_content += f"\n    --early_stopping_patience {args.early_stopping_patience} \\"
        readme_content += f"\n    --early_stopping_threshold {args.early_stopping_threshold} \\"
    if hasattr(args, 'resume_from_checkpoint') and args.resume_from_checkpoint:
        readme_content += f'\n    --resume_from_checkpoint "{args.resume_from_checkpoint}" \\'
    
    # Remove the trailing backslash from the last line
    readme_content = readme_content.rstrip(" \\")
    readme_content += "\n```\n"
    
    # Write README file
    readme_path = os.path.join(args.output_dir, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)
    
    print(f"Created training configuration README: {readme_path}")

def print_training_config(args, resume_checkpoint_path=None):
    """Print training configuration summary."""
    print("\n" + "="*60)
    print("TRAINING CONFIGURATION SUMMARY")
    print("="*60)
    print(f"Model size: {args.model_size}")
    print(f"Pre-processed dataset: {args.dataset_path}")
    print(f"Output: {args.output_dir}")
    if resume_checkpoint_path:
        print(f"Resume from: {resume_checkpoint_path}")
    elif args.resume_from_checkpoint:
        print(f"Resume setting: {args.resume_from_checkpoint}")
    print("-"*60)
    print("Core Training Parameters:")
    print(f"  Learning rate: {args.learning_rate}")
    print(f"  Batch size per device: {args.per_device_train_batch_size}")
    print(f"  Gradient accumulation: {args.gradient_accumulation_steps}")
    print(f"  Effective batch size: {args.per_device_train_batch_size * args.gradient_accumulation_steps * max(1, args.num_gpus)}")
    print(f"  Max steps: {args.max_steps}")
    print(f"  Warmup steps: {args.warmup_steps}")
    print(f"  LR scheduler: {args.lr_scheduler_type}")
    print("-"*60)
    print("Stability Measures:")
    print(f"  Gradient clipping: {args.max_grad_norm if args.max_grad_norm > 0 else 'Disabled'}")
    print(f"  Weight decay: {args.weight_decay}")
    print(f"  Early stopping: {'Enabled' if args.early_stopping_patience > 0 else 'Disabled'}")
    if args.early_stopping_patience > 0:
        print(f"    Patience: {args.early_stopping_patience} steps")
        print(f"    Threshold: {args.early_stopping_threshold}")
    print("-"*60)
    print("Hardware:")
    print(f"  GPUs: {args.num_gpus}")
    # Determine actual precision based on GPU availability
    if args.num_gpus > 0:
        precision = 'BF16' if args.bf16 else 'FP16' if args.fp16 else 'FP32'
    else:
        precision = 'FP32 (CPU)'
    print(f"  Precision: {precision}")
    print(f"  Data workers: {args.dataloader_num_workers}")
    print("-"*60)
    print("Data:")
    print(f"  Using pre-processed log-Mel spectrograms")
    print(f"  Skipping audio preprocessing step")
    print("="*60)

# ============================================================================
# Main Function
# ============================================================================

def main():
    """Main training pipeline for pre-processed data with stability improvements."""
    print("=== Fine-tuning Whisper from Pre-processed Log-Mel Spectrograms ===")
    
    # Parse command line arguments
    args = parse_arguments()
    
    # Resolve checkpoint path for resumption
    resume_checkpoint_path = resolve_checkpoint_path(args.resume_from_checkpoint, args.output_dir)
    
    # Print configuration summary
    print_training_config(args, resume_checkpoint_path)
    
    # Setup GPU environment
    if args.num_gpus > 0:
        if not torch.cuda.is_available():
            print("Warning: CUDA not available, falling back to CPU")
            args.num_gpus = 0
        else:
            available_gpus = torch.cuda.device_count()
            if args.num_gpus > available_gpus:
                print(f"Warning: Requested {args.num_gpus} GPUs, but only {available_gpus} available")
                args.num_gpus = available_gpus
            print(f"Using {args.num_gpus} GPU(s) for training")
            
            # Clear GPU memory before starting
            torch.cuda.empty_cache()
            
            # Set memory fraction to avoid full GPU usage
            for i in range(args.num_gpus):
                torch.cuda.set_per_process_memory_fraction(0.95, device=i)
            
            # Set CUDA_VISIBLE_DEVICES to limit GPU usage
            if args.num_gpus < available_gpus:
                import os
                os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(args.num_gpus))
    
    # Construct model name
    model_name = f"openai/whisper-{args.model_size}"
    print(f"Using model: {model_name}")
    
    # Step 1: Load and validate pre-processed dataset
    dataset = load_and_validate_dataset(args.dataset_path, model_name)
    
    # Step 2: Load Whisper components (tokenizer needed for labels and processor for data collator)
    tokenizer = load_tokenizer(model_name)
    processor = create_processor(model_name)
    
    # Note: No data preprocessing step - dataset is already prepared!
    print("\nSkipping audio preprocessing - using pre-processed log-Mel spectrograms")
    
    # Step 3: Load model
    model = load_pretrained_model(model_name)
    
    # Step 4: Create data collator
    decoder_start_token_id = model.config.decoder_start_token_id
    if decoder_start_token_id is None:
        decoder_start_token_id = tokenizer.convert_tokens_to_ids("<|startoftranscript|>")
    
    data_collator = DataCollatorSpeechSeq2SeqWithPadding(
        processor=processor,
        decoder_start_token_id=decoder_start_token_id,
    )
    
    # Step 5: Load metric and create compute_metrics
    metric = load_metric()
    compute_metrics = create_compute_metrics_function(tokenizer, metric)
    
    # Step 6: Create training arguments
    training_args = create_training_arguments(args)
    
    # Step 6.5: Create README.md with training configuration
    create_training_readme(args)
    
    # Step 7: Create trainer
    trainer = create_trainer(
        training_args=training_args,
        model=model,
        train_dataset=dataset["train"],
        eval_dataset=dataset["test"],
        data_collator=data_collator,
        compute_metrics=compute_metrics,
        processor=processor,
        args=args,
    )
    
    # Step 8: Train
    train_model(trainer, resume_checkpoint_path)
    
    print("=== Training completed successfully! ===")
    print(f"Model saved to: {args.output_dir}")
    print(f"TensorBoard logs: {args.output_dir}/runs/")

if __name__ == "__main__":
    main()
