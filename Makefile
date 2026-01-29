.PHONY: yamlfmt validate help

help:
	@echo "Targets:"
	@echo "  make validate   Run all GitOps config checks"

ROOT := $(shell git rev-parse --show-toplevel)

# -------- FORMAT (auto-fix YAML) --------
format:
	@echo "Applying Kubernetes YAML style with yamlfmt..."
	@cd $(ROOT) && \
	find . -type f \( -name "*.yaml" -o -name "*.yml" \) -print0 \
	| xargs -0 -r yamlfmt -conf "$(ROOT)/.yamlfmt"

	@echo "YAML formatting complete."


# -------- FORMAT CHECK (CI mode) --------
format-check:
	@echo "Checking YAML formatting..."
	@cd $(ROOT) && \
	find . -type f \( -name "*.yaml" -o -name "*.yml" \) -print0 \
	| xargs -0 yamlfmt -lint -conf "$(ROOT)/.yamlfmt"
	@echo "Format check passed."

validate:
	@bash scripts/verify.sh