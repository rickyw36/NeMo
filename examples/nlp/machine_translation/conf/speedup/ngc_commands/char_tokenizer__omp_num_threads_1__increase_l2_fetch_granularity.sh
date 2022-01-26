WANDB_API_KEY="$1"

read -r -d '' command << EOF
set -e -x
export OMP_NUM_THREADS=1
mkdir /result/nemo_experiments
git clone https://github.com/NVIDIA/NeMo
cd NeMo
git checkout iwslt_cascade
pip install -r requirements/requirements.txt
pip install -r requirements/requirements_lightning.txt
pip install -r requirements/requirements_test.txt
pip install -r requirements/requirements_nlp.txt
export PYTHONPATH="\$(pwd)"
cd examples/nlp/machine_translation
wandb login ${WANDB_API_KEY}
python create_autoregressive_char_vocabulary.py \
  --input /data/train/autoregressive_labels.txt \
  --output /workspace/autoregressive_char_vocab.txt \
  --characters_to_exclude $'\n' \
  --eos_token EOS \
  --pad_token PAD
python enc_dec_nmt.py \
  --config-path=conf/speedup \
  --config-name only_char_tokenizer \
  trainer.gpus=1 \
  model.increase_l2_fetch_granularity=true \
  exp_manager.wandb_logger_kwargs.name=char_tokenizer__omp_num_threads_1__increase_l2_fetch_granularity

set +e +x
EOF

ngc batch run \
  --instance dgx1v.16g.1.norm \
  --name "ml-model.aayn speedup_char_tokenizer__omp_num_threads_1__increase_l2_fetch_granularity" \
  --image "nvidia/pytorch:21.08-py3" \
  --result /result \
  --datasetid 88728:/data \
  --commandline "${command}"