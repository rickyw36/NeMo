work_dir=/media/apeganov/DATA/punctuation_and_capitalization/all_punc_no_u/3_128/wiki_wmt_18.01.2022
output_dir="${work_dir}/inference_on_IWSLT_tst2019_results"
model_name=all_punc_no_u_nmt_wiki_wmt_news_crawl_large6x6_bs400000_steps400000_lr2e-4
python punctuate_capitalize_nmt.py \
  --input_text "${work_dir}/for_upload/IWSLT_tst2019/input.txt" \
  --output_text "${output_dir}/${model_name}_with_adjustment_text.txt" \
  --no_all_upper_label \
  --make_queries_contain_intact_sentences \
  --output_labels "${output_dir}/${model_name}_with_adjustment_labels.txt" \
  --model_path "~/NWInf_results/autoregressive_punctuation_capitalization/${model_name}/checkpoints/AAYNLarge6x6.nemo" \
  --max_seq_length 128 \
  --step 126 \
  --margin 0 \
  --add_source_num_words_to_batch