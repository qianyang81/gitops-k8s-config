# ------------------------------
# Config
# ------------------------------
.DEFAULT_GOAL := help

ROOT        := $(shell git rev-parse --show-toplevel)
SCRIPTS_DIR := $(ROOT)/scripts
OUT         ?= out
SEARCH      ?= clusters

VENV := $(ROOT)/.venv
PY   := $(VENV)/bin/python3
PIP  := $(PY) -m pip

YAML_FILES := $(shell find $(ROOT) -type f \( -name "*.yaml" -o -name "*.yml" \))

DAG_ARGS := --root "$(ROOT)" --search "$(SEARCH)" --out-dir "$(OUT)"

.PHONY: help tools format format-check lint lint-yaml validate \
        py-tools venv py-deps py-deps-dev \
        build-dag build-dag-dot build-dag-png \
        graphviz-tools validate-flux find-repo-orphan flux-tools clean

# ------------------------------
# Helpers
# ------------------------------
define require_tool
	@command -v $(1) >/dev/null 2>&1 || { echo "❌ $(1) not installed"; exit 1; }
endef

help:
	@echo "Targets:"
	@echo "  make tools           Check required YAML tools"
	@echo "  make format          Auto-fix YAML style"
	@echo "  make format-check    Check YAML style (CI)"
	@echo "  make lint            Run YAML lint checks"
	@echo "  make build-dag       Generate DAG output"
	@echo "  make build-dag-dot   Generate DOT output"
	@echo "  make build-dag-png   Generate DOT + render PNG (Graphviz)"
	@echo "  make validate        Run checks (format-check + lint + build-dag + validate-flux)"
	@echo "  make validate-flux   Flux build each topology node + kubeconform schema validation"
	@echo "  make clean           Remove local outputs/venv artifacts"

# ------------------------------
# YAML formatting / lint
# ------------------------------
tools:
	$(call require_tool,yamlfmt)
	$(call require_tool,yamllint)
	@echo "✅ YAML tools ok."

format: tools
	@echo "Applying YAML style with yamlfmt..."
	@yamlfmt -conf "$(ROOT)/.yamlfmt"
	@echo "✅ YAML formatting complete."

format-check: tools
	@echo "Checking YAML formatting..."
	@yamlfmt -lint -conf "$(ROOT)/.yamlfmt"
	@echo "✅ Format check passed."

lint-yaml: tools
	@echo "Running yamllint..."
	@yamllint -c "$(ROOT)/.yamllint" $(YAML_FILES)
	@echo "✅ YAML lint passed."

lint: lint-yaml

# ------------------------------
# Python venv + deps (DAG generator)
# ------------------------------
py-tools:
	$(call require_tool,python3)
	@echo "✅ Found python3: $$(command -v python3)"

venv: py-tools
	@test -d "$(VENV)" || python3 -m venv "$(VENV)"
	@$(PIP) install -U pip >/dev/null

py-deps: venv
	@echo "Installing deps from: $(SCRIPTS_DIR)/requirements.txt"
	@$(PIP) install -r "$(SCRIPTS_DIR)/requirements.txt"

py-deps-dev: venv
	@echo "Installing dev-deps from: $(SCRIPTS_DIR)/requirements-dev.txt"
	@$(PIP) install -r "$(SCRIPTS_DIR)/requirements-dev.txt"

# ------------------------------
# DAG generation
# ------------------------------
build-dag: py-deps
	@echo "Generating DAG topologies..."
	@$(PY) "$(SCRIPTS_DIR)/build-dag.py" $(DAG_ARGS)

build-dag-dot: py-deps
	@$(PY) "$(SCRIPTS_DIR)/build-dag.py" $(DAG_ARGS) --dot

# ------------------------------
# Graphviz (DOT -> PNG)
# ------------------------------
graphviz-tools:
	$(call require_tool,dot)
	@echo "✅ Found graphviz: $$(command -v dot)"

build-dag-png: graphviz-tools build-dag-dot
	@echo "Rendering PNG from DOT..."
	@DOT_FILE="$$(ls -1 "$(OUT)"/*.dot 2>/dev/null | head -n 1)"; \
	if [ -z "$$DOT_FILE" ]; then \
	  echo "❌ No .dot file found in $(OUT). Did build-dag-dot generate it?"; \
	  exit 1; \
	fi; \
	PNG_FILE="$${DOT_FILE%.dot}.png"; \
	dot -Tpng "$$DOT_FILE" -o "$$PNG_FILE"; \
	echo "✅ Wrote $$PNG_FILE"

# ------------------------------
# Flux + kubeconform validation tools
# ------------------------------
flux-tools:
	$(call require_tool,flux)
	$(call require_tool,kubeconform)
	@echo "✅ Flux/kubeconform tools ok."


# ------------------------------
# Aggregate checks
# ------------------------------
PLAN               ?= $(OUT)/plan.json
RENDERED_DIR       ?= $(OUT)/rendered
VALIDATE_JOBS      ?= 3
KUBECONFORM_K8SVER ?= master

validate-flux: flux-tools build-dag
	@echo "Validating Flux topologies (flux build + kubeconform)..."
	@rm -rf "$(RENDERED_DIR)"
	@$(PY) "$(SCRIPTS_DIR)/validate-flux-dag.py" \
		--root "$(ROOT)" \
		--plan "$(PLAN)" \
		--artifacts-dir "$(RENDERED_DIR)" \
		--jobs "$(VALIDATE_JOBS)" \
		--ignore-missing-schemas \
		--kubernetes-version "$(KUBECONFORM_K8SVER)" 

find-repo-orphan:
	@echo "Finding orphan yaml files in the repo..."
	@$(PY) "$(SCRIPTS_DIR)/find-repo-orphan-kustomize.py" \
		--mode warn \
		--no-use-git

find-repo-orphan-ci:
	@echo "Finding orphan yaml files in the repo..."
	@$(PY) "$(SCRIPTS_DIR)/find-repo-orphan-kustomize.py" \
		--mode warn \
		--no-use-git

validate: format-check lint validate-flux

# ------------------------------
# Cleanup
# ------------------------------
clean:
	@rm -rf "$(OUT)"
	@rm -rf "$(VENV)"
	@echo "✅ Cleaned $(OUT) and $(VENV)"
