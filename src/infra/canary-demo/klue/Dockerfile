ARG SERVER_IMAGE=voc-klue-api:eaf3b9c
FROM ${SERVER_IMAGE}
ARG ANALYSIS_KINDS
ENV MODEL_PATH=/opt/model MODEL_DOMAIN_MAP=/config/domains.json MODEL_ANALYSIS_KINDS=${ANALYSIS_KINDS} \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false HF_HOME=/tmp/huggingface
USER root
RUN install -d -m 0755 /opt/model /config
COPY --from=mapping domains.json /config/domains.json
COPY config.json model.safetensors tokenizer.json tokenizer_config.json special_tokens_map.json vocab.txt /opt/model/
RUN chmod 0555 /opt/model /config && chmod 0444 /opt/model/* /config/domains.json
USER 10001
