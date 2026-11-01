FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS weights
WORKDIR /download
COPY ops/docker/download_model.py .
ARG MODEL_VARIANT=8b
RUN MODEL_VARIANT=${MODEL_VARIANT} python download_model.py

FROM ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:b4e561932a1dd460ec2f9d2875ad0d9535cbfd3d2d6a48bdb3c9d50804088bff
COPY --from=weights /download/model.gguf /models/model.gguf
