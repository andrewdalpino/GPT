import time
import random
import warnings
from os import path, environ
from argparse import ArgumentParser
from contextlib import nullcontext

import torch

from torch.utils.data import DataLoader
from torch.optim import AdamW
from torch.amp import autocast
from torch.cuda import set_device, is_available as cuda_is_available, is_bf16_supported
from torch.nn.utils import clip_grad_norm_
from torch.nn.parallel import DistributedDataParallel
from torch.distributed import init_process_group, destroy_process_group

from torchmetrics.text import Perplexity

from model import GPT
from data import OpenwebtextDataset

from tqdm import tqdm

RANK = int(environ.get("RANK", -1))
LOCAL_RANK = int(environ.get("LOCAL_RANK", -1))
WORLD_SIZE = int(environ.get("WORLD_SIZE", -1))

IS_DDP = RANK >= 0

IS_MASTER = RANK == 0 or not IS_DDP

DDP_BACKEND = "nccl"  # gloo, nccl, etc.


def main():
    parser = ArgumentParser(description="Train the GPT.")

    parser.add_argument("--batch_size", default=4, type=int)
    parser.add_argument("--gradient_accumulation_steps", default=32, type=int)
    parser.add_argument("--max_samples_per_epoch", default=4096, type=int)
    parser.add_argument("--learning_rate", default=5e-4, type=float)
    parser.add_argument("--dropout", default=0.1, type=float)
    parser.add_argument("--max_gradient_norm", default=1.0, type=float)
    parser.add_argument("--num_epochs", default=1000, type=int)
    parser.add_argument("--eval_epochs", default=10, type=int)
    parser.add_argument("--block_size", default=1024, type=int)
    parser.add_argument("--embedding_dimensions", default=768, type=int)
    parser.add_argument("--num_attention_heads", default=12, type=int)
    parser.add_argument("--num_hidden_layers", default=12, type=int)
    parser.add_argument("--checkpoint_epochs", default=20, type=int)
    parser.add_argument("--checkpoint_path", default="./out/checkpoint.pt", type=str)
    parser.add_argument("--dataset_path", default="./dataset", type=str)
    parser.add_argument("--num_dataset_processes", default=4, type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--device", default="cuda", type=str)
    parser.add_argument("--seed", default=None, type=int)

    args = parser.parse_args()

    if args.batch_size < 1:
        raise ValueError(f"Batch size must be greater than 0, {args.batch_size} given.")

    if args.gradient_accumulation_steps < 1:
        raise ValueError(
            f"Gradient accumulation steps must be greater than 0, {args.gradient_accumulation_steps} given."
        )

    if args.learning_rate < 0:
        raise ValueError(
            f"Learning rate must be a positive value, {args.learning_rate} given."
        )

    if args.num_epochs < 1:
        raise ValueError(f"Must train for at least 1 epoch, {args.num_epochs} given.")

    if args.eval_epochs < 1:
        raise ValueError(
            f"Eval epochs must be greater than 0, {args.eval_epochs} given."
        )

    if args.checkpoint_epochs < 1:
        raise ValueError(
            f"Checkpoint epochs must be greater than 0, {args.checkpoint_epochs} given."
        )

    if "cuda" in args.device and not cuda_is_available():
        raise RuntimeError("Cuda is not available.")

    torch.set_float32_matmul_precision("high")

    if IS_DDP:
        init_process_group(backend=DDP_BACKEND, world_size=WORLD_SIZE)

        args.device = f"cuda:{LOCAL_RANK}"

        set_device(args.device)

        if args.gradient_accumulation_steps % WORLD_SIZE != 0:
            warnings.warn(
                "Gradient accumulation steps does not divide equally into the world size."
            )

        args.gradient_accumulation_steps //= WORLD_SIZE

        if args.seed:
            args.seed += RANK

    dtype = (
        torch.bfloat16
        if args.device == "cuda" and is_bf16_supported()
        else torch.float32
    )

    forward_context = autocast(device_type=args.device, dtype=dtype)

    if args.seed:
        torch.manual_seed(args.seed)
        random.seed(args.seed)

    train = OpenwebtextDataset(
        root_path=args.dataset_path,
        train=True,
        block_size=args.block_size,
        max_samples_per_epoch=args.max_samples_per_epoch,
        num_processes=args.num_dataset_processes,
    )

    test = OpenwebtextDataset(
        root_path=args.dataset_path,
        train=False,
        block_size=args.block_size,
        max_samples_per_epoch=args.max_samples_per_epoch,
        num_processes=args.num_dataset_processes,
    )

    train_loader = DataLoader(train, batch_size=args.batch_size, pin_memory=True)
    test_loader = DataLoader(test, batch_size=args.batch_size, pin_memory=True)

    model_args = {
        "vocabulary_size": train.VOCABULARY_SIZE,
        "block_size": args.block_size,
        "embedding_dimensions": args.embedding_dimensions,
        "num_heads": args.num_attention_heads,
        "num_layers": args.num_hidden_layers,
        "dropout": args.dropout,
    }

    model = GPT(**model_args).to(args.device)

    optimizer = AdamW(model.parameters(), lr=args.learning_rate, fused=True)

    perplexity_metric = Perplexity(ignore_index=-1).to(args.device)

    print("Compiling model")
    model = torch.compile(model)

    print(f"Model has {model.num_trainable_params:,} trainable parameters")

    if args.resume:
        checkpoint = torch.load(
            args.checkpoint_path, map_location=args.device, weights_only=True
        )

        model.load_state_dict(checkpoint["model"])
        optimizer.load_state_dict(checkpoint["optimizer"])

        print("Previous checkpoint resumed successfully")

    if IS_DDP:
        model = DistributedDataParallel(
            model,
            device_ids=[LOCAL_RANK],
        )

    model.train()

    print("Training ...")

    for epoch in range(1, args.num_epochs + 1):
        total_cross_entropy, total_gradient_norm = 0.0, 0.0
        total_batches, total_steps = 0, 0

        for step, (x, y) in enumerate(
            tqdm(train_loader, desc=f"Epoch {epoch}", leave=False), start=1
        ):
            x = x.to(args.device, non_blocking=True)
            y = y.to(args.device, non_blocking=True)

            with forward_context:
                y_pred, loss = model(x, y)

                loss /= args.gradient_accumulation_steps

            with (
                model.no_sync()
                if IS_DDP and step != args.gradient_accumulation_steps
                else nullcontext()
            ):
                loss.backward()

            total_cross_entropy += loss.item()

            if step % args.gradient_accumulation_steps == 0:
                norm = clip_grad_norm_(model.parameters(), args.max_gradient_norm)

                optimizer.step()

                optimizer.zero_grad(set_to_none=True)

                total_gradient_norm += norm.item()
                total_steps += 1

            total_batches += 1

        average_cross_entropy = total_cross_entropy / total_batches
        average_gradient_norm = total_gradient_norm / total_steps

        print(
            f"Epoch {epoch}:",
            f"Cross Entropy: {average_cross_entropy:.5f},",
            f"Gradient Norm: {average_gradient_norm:.4f}",
        )

        if epoch % args.eval_epochs == 0 and IS_MASTER:
            model.eval()

            for x, y in tqdm(test_loader, desc="Testing", leave=False):
                x = x.to(args.device, non_blocking=True)
                y = y.to(args.device, non_blocking=True)

                with torch.no_grad():
                    with forward_context:
                        y_pred, _ = model(x)

                    perplexity_metric.update(y_pred, y)

            average_perplexity = perplexity_metric.compute().item()

            print(f"Perplexity: {average_perplexity:.4f}")

            perplexity_metric.reset()

            model.train()

        if epoch % args.checkpoint_epochs == 0 and IS_MASTER:
            checkpoint = {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "model_args": model_args,
            }

            torch.save(checkpoint, args.checkpoint_path)

            print("Checkpoint saved")

    if IS_DDP:
        destroy_process_group()


if __name__ == "__main__":
    main()
