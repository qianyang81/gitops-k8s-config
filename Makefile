.PHONY: format format-check lint lint-yaml validate help

help:
	@echo "Targets:"
	@echo "  make tools         Check required tools"
	@echo "  make format        Auto-fix YAML style"
	@echo "  make format-check  Check YAML style (CI)"
	@echo "  make lint          Run YAML lint checks"
	@echo "  make validate      Run all GitOps quick checks"

ROOT := $(shell git rev-parse --show-toplevel)
# -------- YAML FILE LIST --------
YAML_FILES := $(shell find $(ROOT) -type f \( -name "*.yaml" -o -name "*.yml" \))

# -------- TOOL CHECK --------
tools:
	@command -v yamlfmt >/dev/null 2>&1 || { echo "❌ yamlfmt not installed"; exit 1; }
	@command -v yamllint >/dev/null 2>&1 || { echo "❌ yamllint not installed"; exit 1; }
	@echo "✅ All required tools are installed."
	
# -------- FORMAT (auto-fix YAML style) --------
format: tools
	@echo "Applying Kubernetes YAML style with yamlfmt..."
	yamlfmt -conf "$(ROOT)/.yamlfmt" $(YAML_FILES)
	@echo "YAML formatting complete."

# -------- FORMAT CHECK (CI mode) --------
format-check: tools
	@echo "Checking YAML formatting..."
	yamlfmt -lint -conf "$(ROOT)/.yamlfmt" $(YAML_FILES)
	@echo "Format check passed."

# -------- YAML LINT (syntax + structure) --------
lint-yaml: tools
	@echo "Running yamllint..."
	yamllint -c "$(ROOT)/.yamllint" $(YAML_FILES)
	@echo "YAML lint passed."

# -------- LINT (aggregate) --------
lint: lint-yaml

validate:
	@bash scripts/verify.sh