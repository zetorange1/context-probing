import argparse
from pathlib import Path
import sys

import datasets
import numpy as np
import transformers


def _get_windows_batched(examples, window_len, pad_id):
    return {
        k: [
            t[i][j : j + window_len] + [
                pad_id if k == "input_ids" else
                0 if type(t[i][0]) == int else
                None
            ] * (j + window_len - len(t[i]))
            for i in range(len(examples["input_ids"]))
            for j in range(len(examples["input_ids"][i]) - 1)
        ]
        for k, t in examples.items()
    }


def _add_labels(example):
    example["labels"] = example["input_ids"].copy()
    return example


def _add_positions(example):
    example["positions"] = np.arange(len(example["input_ids"]))
    return example


def _add_seq_ids(example, idx):
    example["seq_id"] = [idx] * len(example["input_ids"])
    return example


def get_data(
    path, pad_id=-1, window_len=None, columns=None, add_positions=False, add_seq_ids=False, num_proc=16
):
    dataset = datasets.Dataset.load_from_disk(path)
    if columns is not None:
        dataset = dataset.remove_columns(
            [col for col in dataset.column_names if col not in columns]
        )
    dataset = dataset.map(_add_labels, num_proc=num_proc)
    if add_positions:
        dataset = dataset.map(_add_positions, num_proc=num_proc)
    if add_seq_ids:
        dataset = dataset.map(_add_seq_ids, num_proc=num_proc, with_indices=True)
    if window_len is not None:
        dataset = dataset.map(
            _get_windows_batched, fn_kwargs=dict(window_len=window_len, pad_id=pad_id),
            batched=True, batch_size=1, num_proc=num_proc
        )
    return dataset


def get_shard_sizes(dataset, total_shards):
    return [
        len(dataset) // total_shards + int(i < len(dataset) % total_shards)
        for i in range(total_shards)
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data_path", type=str)
    parser.add_argument("output_path_prefix", type=str)
    parser.add_argument("--model-path", type=str, required=True)
    parser.add_argument("--window-len", type=int, default=2048)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--num-proc", type=int, default=16)
    parser.add_argument("--total-shards", type=int, default=1)
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--out-dtype", type=np.dtype, default=np.float16)
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()

    datasets.disable_caching()

    out_path = Path(f"{args.output_path_prefix}-{args.shard_id:05d}-of-{args.total_shards:05d}")
    if args.skip_existing and out_path.exists():
        print(f"Output file {out_path} exists, exiting.", file=sys.stderr)
        return

    # Prepare the data
    tokenizer = transformers.AutoTokenizer.from_pretrained(args.model_path)
    dataset = get_data(
        args.data_path,
        columns=["input_ids", "attention_mask"],
        window_len=args.window_len,
        num_proc=args.num_proc,
        pad_id=(
            tokenizer.pad_token_id if tokenizer.pad_token_id is not None
            else tokenizer.eos_token_id
        ),
    )

    # Figure out which examples belong in this shard
    if args.shard_id not in range(args.total_shards):
        raise ValueError(f"Invalid shard index {args.shard_id}")
    # There are (len(dataset) % total_shards) shards that have 1 extra example
    shard_sizes = get_shard_sizes(dataset, args.total_shards)[:args.shard_id + 1]
    data_range = range(sum(shard_sizes[:-1]), sum(shard_sizes))
    dataset = dataset.select(data_range)

    # Load and run the model
    model = transformers.AutoModelForCausalLM.from_pretrained(args.model_path)
    trainer = transformers.Trainer(
        args=transformers.TrainingArguments(
            output_dir="/tmp/trainer_output",
            eval_accumulation_steps=1,
            per_device_eval_batch_size=args.batch_size,
            no_cuda=True),
        model=model,
    )
    results = trainer.predict(
        dataset, ignore_keys=["past_key_values", "hidden_states", "attentions"]
    )

    # Save the predictions
    try:
        np.save(out_path, results.predictions.astype(args.out_dtype), allow_pickle=False)
    except:
        try:
            out_path.unlink(missing_ok=True)
        finally:
            raise
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()