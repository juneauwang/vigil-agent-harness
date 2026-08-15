#!/bin/bash
# ============================================================================
# Vigil Agent — 一键安装脚本（源码安装 / 自托管）
# ============================================================================
# 对应 README Roadmap：「一键安装脚本 setup-vigil.sh（源码安装/自托管路径：
# clone → 装依赖 → 初始化）」。基于 upstream 的 setup-hermes.sh 改造：
# 入口换成 vigil，初始化换成 vigil ops-init。
#
# 用法（两种）：
#   1. 已 clone 仓库，在仓库根目录直接跑：
#        ./setup-vigil.sh [--no-init]
#   2. 未 clone，远程一键（自动 clone 到 ~/.vigil/vigil-agent-harness）：
#        bash <(curl -fsSL https://raw.githubusercontent.com/juneauwang/vigil-agent-harness/main/setup-vigil.sh)
#
# 选项：
#   --no-init        跳过 vigil ops-init（默认：交互式询问）
#   --dir PATH       源码目录（未 clone 时 clone 到该目录；已是 checkout 时直接安装）
#   --branch BRANCH  clone 时使用的分支（默认 main）
#   -h, --help       显示帮助
#
# 流程：
#   定位/克隆源码 → uv + Python 3.11 → .venv → 依赖（uv.lock 优先，失败回退）
#   → 软链 vigil 命令 → 同步内置 skills → 可选 ops-init → 下一步提示
# ============================================================================

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

# --- 默认值 ----------------------------------------------------------------
REPO_URL="https://github.com/juneauwang/vigil-agent-harness.git"
DEFAULT_SRC_DIR="${VIGIL_DIR:-$HOME/.vigil/vigil-agent-harness}"
PYTHON_VERSION="3.11"
BRANCH="main"
RUN_INIT=true
UV_CMD=""
VIGIL_ROOT=""

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.
export UV_NO_CONFIG=1

# --- 输出辅助 --------------------------------------------------------------
log_info()  { echo -e "${CYAN}→${NC} $*"; }
log_ok()    { echo -e "${GREEN}✓${NC} $*"; }
log_warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
log_error() { echo -e "${RED}✗${NC} $*"; }

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

is_termux() {
    [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *"com.termux/files/usr"* ]]
}

get_command_link_dir() {
    if is_termux && [ -n "${PREFIX:-}" ]; then
        echo "$PREFIX/bin"
    else
        echo "$HOME/.local/bin"
    fi
}

get_command_link_display_dir() {
    if is_termux && [ -n "${PREFIX:-}" ]; then
        echo '$PREFIX/bin'
    else
        echo '~/.local/bin'
    fi
}

# 交互式 yes/no；非交互（curl | bash 管道）时按默认值直接返回，
# 避免 read 吞掉脚本剩余输入。
prompt_yes() {
    local default="$1" question="$2"
    if [ ! -t 0 ]; then
        [ "$default" = "y" ]
        return
    fi
    local answer
    read -r -p "$question [Y/n] " answer
    case "$answer" in
        ""|y|Y) return 0 ;;
        *) return 1 ;;
    esac
}

is_checkout() {
    local dir="$1"
    [ -f "$dir/pyproject.toml" ] && grep -q '^name = "vigil-agent-harness"' "$dir/pyproject.toml"
}

resolve_data_root() {
    # 与 hermes_constants.get_hermes_home() 的默认链保持一致：
    # VIGIL_HOME → ~/.vigil
    if [ -n "${VIGIL_HOME:-}" ]; then
        VIGIL_ROOT="$VIGIL_HOME"
    else
        VIGIL_ROOT="$HOME/.vigil"
    fi
}

# --- 源码定位 / 克隆 ---------------------------------------------------------
ensure_git() {
    if command -v git >/dev/null 2>&1; then
        return 0
    fi
    log_warn "未找到 git，尝试自动安装..."
    if is_termux; then
        pkg install -y git >/dev/null 2>&1 || true
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y -qq git >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y -q git >/dev/null 2>&1 || true
    elif command -v brew >/dev/null 2>&1; then
        brew install git >/dev/null 2>&1 || true
    fi
    if ! command -v git >/dev/null 2>&1; then
        log_error "git 不可用，请先手动安装 git（Debian/Ubuntu: sudo apt install git）后重试"
        exit 1
    fi
}

clone_repo() {
    local dir="$1"
    if [ -d "$dir/.git" ]; then
        log_info "已存在源码 checkout：$dir（跳过 clone）"
        return
    fi
    ensure_git
    mkdir -p "$(dirname "$dir")"
    log_info "clone 源码（--depth 1 --branch $BRANCH）到 $dir ..."
    if git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$dir" 2>/dev/null; then
        return
    fi
    rm -rf "$dir" 2>/dev/null || true
    log_warn "浅 clone 失败，改用完整 clone ..."
    git clone "$REPO_URL" "$dir"
}

resolve_source() {
    local script_dir="$1" install_dir_arg="$2"
    if is_checkout "$script_dir"; then
        SOURCE_DIR="$script_dir"
        log_info "检测到 Vigil 源码 checkout：$SOURCE_DIR"
    elif [ -n "$install_dir_arg" ] && is_checkout "$install_dir_arg"; then
        SOURCE_DIR="$install_dir_arg"
        log_info "使用指定源码目录：$SOURCE_DIR"
    else
        SOURCE_DIR="${install_dir_arg:-$DEFAULT_SRC_DIR}"
        clone_repo "$SOURCE_DIR"
    fi
    cd "$SOURCE_DIR"
    SOURCE_DIR="$(pwd)"  # 归一化绝对路径（--dir 可能是相对路径）
    VIGIL_VERSION="$(grep -m1 '^version = ' pyproject.toml | sed 's/^version = "\(.*\)"/\1/')"
}

# --- uv / Python / venv ------------------------------------------------------
ensure_uv() {
    if is_termux; then
        # Termux 用 Python stdlib venv + pip，不走 uv
        return
    fi
    if command -v uv >/dev/null 2>&1; then
        UV_CMD="uv"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        UV_CMD="$HOME/.local/bin/uv"
    elif [ -x "$HOME/.cargo/bin/uv" ]; then
        UV_CMD="$HOME/.cargo/bin/uv"
    fi
    if [ -n "$UV_CMD" ]; then
        log_ok "uv 已就绪（$($UV_CMD --version)）"
        return
    fi
    log_info "安装 uv ..."
    # 两段式安装：避免 `curl | sh` 掩盖 curl 失败（sh 在空 stdin 下 exit 0）
    local _log _installer
    _log="$(mktemp 2>/dev/null || echo "/tmp/vigil-uv-install.$$.log")"
    _installer="$(mktemp 2>/dev/null || echo "/tmp/vigil-uv-installer.$$.sh")"
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$_installer" 2>"$_log"; then
        log_error "下载 uv 安装器失败。请手动安装：https://docs.astral.sh/uv/"
        sed 's/^/    /' "$_log" >&2
        rm -f "$_log" "$_installer"
        exit 1
    fi
    if ! sh "$_installer" >>"$_log" 2>&1; then
        log_error "uv 安装失败。请手动安装：https://docs.astral.sh/uv/"
        sed 's/^/    /' "$_log" >&2
        rm -f "$_log" "$_installer"
        exit 1
    fi
    rm -f "$_installer"
    if [ -x "$HOME/.local/bin/uv" ]; then
        UV_CMD="$HOME/.local/bin/uv"
    elif [ -x "$HOME/.cargo/bin/uv" ]; then
        UV_CMD="$HOME/.cargo/bin/uv"
    fi
    if [ -z "$UV_CMD" ]; then
        log_error "uv 安装完成但未找到二进制，请把 ~/.local/bin 加入 PATH 后重试"
        rm -f "$_log"
        exit 1
    fi
    rm -f "$_log"
    log_ok "uv 安装完成（$($UV_CMD --version)）"
}

ensure_python() {
    if is_termux; then
        PYTHON_PATH="$(command -v python 2>/dev/null || true)"
        if [ -z "$PYTHON_PATH" ]; then
            log_error "Termux 需要 Python 3.11+：请先运行 pkg install python"
            exit 1
        fi
        if ! "$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            log_error "Termux Python 版本过低（需 3.11+）：请 pkg upgrade python"
            exit 1
        fi
        log_ok "Python：$("$PYTHON_PATH" --version)"
        return
    fi
    if "$UV_CMD" python find "$PYTHON_VERSION" >/dev/null 2>&1; then
        PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION")"
    else
        log_info "未找到 Python $PYTHON_VERSION，用 uv 安装 ..."
        "$UV_CMD" python install "$PYTHON_VERSION"
        PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION")"
    fi
    log_ok "Python：$("$PYTHON_PATH" --version)"
}

create_venv() {
    VENV_DIR="$SOURCE_DIR/.venv"
    if [ -d "$VENV_DIR" ]; then
        log_info "复用已有虚拟环境：$VENV_DIR"
        return
    fi
    log_info "创建虚拟环境（Python $PYTHON_VERSION）..."
    if is_termux; then
        "$PYTHON_PATH" -m venv "$VENV_DIR"
    else
        "$UV_CMD" venv "$VENV_DIR" --python "$PYTHON_VERSION"
    fi
    log_ok "虚拟环境已创建：$VENV_DIR"
}

# --- 依赖安装 ----------------------------------------------------------------
install_deps() {
    export VIRTUAL_ENV="$VENV_DIR"
    local SETUP_PYTHON="$VENV_DIR/bin/python"

    log_info "安装依赖（首次可能需要几分钟）..."
    if is_termux; then
        export ANDROID_API_LEVEL="$(getprop ro.build.version.sdk 2>/dev/null || printf '%s' "${ANDROID_API_LEVEL:-}")"
        "$SETUP_PYTHON" -m pip install --upgrade pip setuptools wheel
        if [ -f "constraints-termux.txt" ]; then
            "$SETUP_PYTHON" -m pip install -e ".[termux]" -c constraints-termux.txt \
                || "$SETUP_PYTHON" -m pip install -e "." -c constraints-termux.txt
        else
            "$SETUP_PYTHON" -m pip install -e ".[termux]" \
                || "$SETUP_PYTHON" -m pip install -e "."
        fi
    else
        # 首选 uv.lock hash 校验安装。Vigil fork 的 uv.lock 根包名仍是上游
        # hermes-agent（改名 vigil-agent-harness 后未重新生成），--locked
        # 校验不过会失败，自动走下面的回退。
        # 回退刻意不用 .[all]：fork 的 [all] extra 仍引用上游 hermes-agent，
        # 会从 PyPI 拉入无关的上游包、覆盖入口；基础 -e . 已覆盖核心 + ops。
        if [ -f "uv.lock" ] && UV_PROJECT_ENVIRONMENT="$VENV_DIR" "$UV_CMD" sync --extra all --locked; then
            log_ok "依赖已安装（uv.lock hash 校验）"
        else
            log_warn "uv.lock 安装不可用，回退到基础安装（uv pip install -e .）"
            "$UV_CMD" pip install -e .
            log_ok "依赖已安装（基础集；其余能力按需 lazy-install）"
        fi
    fi
}

# --- 命令入口 / PATH ---------------------------------------------------------
link_command() {
    local bin_dir
    bin_dir="$(get_command_link_dir)"
    mkdir -p "$bin_dir"
    ln -sf "$VENV_DIR/bin/vigil" "$bin_dir/vigil"
    log_ok "已软链 vigil → $(get_command_link_display_dir)/vigil"
}

ensure_path() {
    if is_termux; then
        return  # $PREFIX/bin 已在 PATH
    fi
    local bin_dir="$HOME/.local/bin"
    if echo "$PATH" | tr ':' '\n' | grep -q "^$bin_dir$"; then
        return
    fi
    SHELL_CONFIG=""
    if [[ "$SHELL" == *"zsh"* ]]; then
        SHELL_CONFIG="$HOME/.zshrc"
    elif [[ "$SHELL" == *"bash"* ]]; then
        SHELL_CONFIG="$HOME/.bashrc"
        [ ! -f "$SHELL_CONFIG" ] && SHELL_CONFIG="$HOME/.bash_profile"
    else
        if [ -f "$HOME/.zshrc" ]; then
            SHELL_CONFIG="$HOME/.zshrc"
        elif [ -f "$HOME/.bashrc" ]; then
            SHELL_CONFIG="$HOME/.bashrc"
        elif [ -f "$HOME/.bash_profile" ]; then
            SHELL_CONFIG="$HOME/.bash_profile"
        fi
    fi
    if [ -n "$SHELL_CONFIG" ]; then
        touch "$SHELL_CONFIG" 2>/dev/null || true
        if ! grep -q '\.local/bin' "$SHELL_CONFIG" 2>/dev/null; then
            printf '\n# Vigil Agent — ensure ~/.local/bin is on PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$SHELL_CONFIG"
            log_ok "已把 ~/.local/bin 加入 PATH（$SHELL_CONFIG）"
        else
            log_ok "~/.local/bin 已在 $SHELL_CONFIG 中"
        fi
    else
        log_warn "未找到 shell 配置文件，请手动把 ~/.local/bin 加入 PATH"
    fi
}

# --- skills 同步 --------------------------------------------------------------
sync_skills() {
    local skills_dir="$VIGIL_ROOT/skills"
    mkdir -p "$skills_dir"
    log_info "同步内置 skills 到 $skills_dir ..."
    if "$VENV_DIR/bin/python" "$SOURCE_DIR/tools/skills_sync.py" 2>/dev/null; then
        log_ok "skills 已同步"
    else
        cp -rn "$SOURCE_DIR/skills/"* "$skills_dir/" 2>/dev/null || true
        log_ok "skills 已复制（回退路径）"
    fi
}

# --- 初始化 ------------------------------------------------------------------
run_ops_init() {
    if [ "$RUN_INIT" = false ]; then
        log_info "已跳过 vigil ops-init（--no-init）"
        return
    fi
    local ops_root="$VIGIL_ROOT/profiles/ops"
    if [ -f "$ops_root/config.yaml" ]; then
        log_info "ops profile 已存在（$ops_root），跳过初始化；如需重建请手动运行 vigil ops-init --force"
        return
    fi
    if prompt_yes y "初始化 ops profile（vigil ops-init，生成拓扑表 + 样例 runbook）？"; then
        "$VENV_DIR/bin/vigil" ops-init
        log_ok "ops profile 已初始化"
    else
        log_info "跳过初始化，稍后手动运行：vigil ops-init"
    fi
}

print_next_steps() {
    echo ""
    echo -e "${GREEN}✓ 安装完成！${NC}"
    echo ""
    echo "下一步："
    if [ -n "$SHELL_CONFIG" ]; then
        echo "  1. 重新加载 shell（或执行 source $SHELL_CONFIG）"
    else
        echo "  1. 确保 ~/.local/bin 在 PATH 中"
    fi
    echo "  2. 配置模型：编辑 $VIGIL_ROOT/profiles/ops/config.yaml 的 model 段"
    echo "  3. 写入 API key：$VIGIL_ROOT/profiles/ops/.env（如 OPENROUTER_API_KEY=...）"
    echo "  4. 启动：vigil -p ops"
    echo ""
    echo "其他命令："
    echo "  vigil doctor        # 诊断配置"
    echo "  vigil status        # 查看配置"
    echo "  vigil ops-init --help"
    echo "  源码目录：$SOURCE_DIR"
    echo ""
}

main() {
    local install_dir_arg=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-init) RUN_INIT=false; shift ;;
            --dir) install_dir_arg="$2"; shift 2 ;;
            --branch) BRANCH="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) usage; exit 1 ;;
        esac
    done

    echo -e "${CYAN}⚕ Vigil Agent 一键安装（源码安装 / 自托管）${NC}"
    echo ""

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    resolve_source "$script_dir" "$install_dir_arg"
    resolve_data_root

    echo -e "${CYAN}⚕ Vigil v${VIGIL_VERSION}${NC}"
    echo ""

    ensure_uv
    ensure_python
    create_venv
    install_deps
    link_command
    ensure_path
    sync_skills
    run_ops_init
    print_next_steps
}

main "$@"
