# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import warnings

import torch
from datasets import load_dataset
from tqdm import tqdm
from transformers import AutoModelForSequenceClassification, AutoTokenizer, HfArgumentParser, DataCollatorWithPadding

from trl import ModelConfig, RewardConfig, RewardTrainer, get_kbit_device_map, get_peft_config, get_quantization_config
from trl.trainer.utils import RewardDataCollatorWithPadding

tqdm.pandas()


if __name__ == "__main__":
    ################
    # Model & Tokenizer
    ################
    base_model = "EleutherAI/pythia-1b-deduped"
    tokenizer = AutoTokenizer.from_pretrained(base_model)
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})
    left_tokenizer = AutoTokenizer.from_pretrained(base_model, padding_side="left") # for generation
    left_tokenizer.pad_token = left_tokenizer.eos_token
    if tokenizer.chat_template is None:
        # a default chat template to simply concatenate the messages
        tokenizer.chat_template = "{% for message in messages %}{{' ' + message['content']}}{% endfor %}{{ eos_token }}"
    model = AutoModelForSequenceClassification.from_pretrained(base_model, num_labels=1)
    model.config.pad_token_id = tokenizer.pad_token_id

    ################
    # Dataset
    ################
    raw_datasets = load_dataset("trl-internal-testing/descriptiveness-sentiment-trl-style", split="descriptiveness")
    def process(row):
        chosen = tokenizer.apply_chat_template(row["chosen"], tokenize=False).strip()
        rejected = tokenizer.apply_chat_template(row["rejected"], tokenize=False).strip()
        row["chosen"] = chosen
        row["rejected"] = rejected
        tokenize_chosen = tokenizer(chosen)
        tokenize_rejected = tokenizer(rejected)
        row["input_ids_chosen"] = tokenize_chosen["input_ids"]
        row["attention_mask_chosen"] = tokenize_chosen["attention_mask"]
        row["input_ids_rejected"] = tokenize_rejected["input_ids"]
        row["attention_mask_rejected"] = tokenize_rejected["attention_mask"]
        return row

    raw_datasets = raw_datasets.map(process, load_from_cache_file=False)
    raw_datasets = raw_datasets.remove_columns(["chosen", "rejected", "prompt"])
    eval_samples = 20
    train_dataset = raw_datasets.select(range(len(raw_datasets) - eval_samples))
    eval_dataset = raw_datasets.select(range(len(raw_datasets) - eval_samples, len(raw_datasets)))
    ################
    # Training
    ################
    training_args = RewardConfig(
        per_device_train_batch_size=16,
        gradient_accumulation_steps=4,
        learning_rate=5e-05,
        logging_steps=1,
        evaluation_strategy="epoch",
        num_train_epochs=1,
        output_dir="minimal/reward",
        report_to=None,
        max_length=512,
    )
    training_args.remove_unused_columns = False
    # treats the EOS token and the padding token distinctively
    default_collator = RewardDataCollatorWithPadding(tokenizer=tokenizer)
    def data_collator(x):
        batch = default_collator(x)
        batch["input_ids_chosen"].masked_fill_(~batch["attention_mask_chosen"].bool(), 0)
        batch["input_ids_rejected"].masked_fill_(~batch["attention_mask_rejected"].bool(), 0)
        return batch
    trainer = RewardTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=data_collator,
    )
    trainer.train()
    trainer.save_model(training_args.output_dir)
    metrics = trainer.evaluate()
    trainer.log_metrics("eval", metrics)
    print(metrics)