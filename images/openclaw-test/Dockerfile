FROM node:24-slim
ENV COREPACK_HOME=/opt/corepack
RUN corepack prepare pnpm@12.15.0 --activate \
    && npm install --prefix /opt/corepack/v1/pnpm/12.15.0 --no-save --ignore-scripts @pnpm/exe.linux-arm64@12.15.0 \
    && chmod -R a+rX /opt/corepack
