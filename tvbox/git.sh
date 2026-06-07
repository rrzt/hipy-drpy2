#!/usr/bin/env bash
# ====================================================
# 项目名称: Git Master 终端可视化管理脚本 (重构优化版)
# 运行环境: 适配 Android / Termux / MT 管理器终端环境
# 核心原则: 独立执行单步操作，采用现代 Git 命令，详细注释
# ====================================================

# ================= 配置区 =================
# 专属 Github 仓库地址与日志绝对路径 (请勿随意修改)
MY_REPO_URL="https://github.com/cluntop/tvbox.git"
LOG_FILE="/storage/emulated/0/box/.github/git.log"

# ================= 颜色与样式 =================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
PURPLE='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m' # 恢复默认配色

# ================= 基础核心函数 =================

# 初始化日志目录 (单独执行，避免与其他命令合并)
init_log_dir() {
    local log_dir
    log_dir=$(dirname "$LOG_FILE")
    if [ ! -d "$log_dir" ]; then
        mkdir -p "$log_dir" 2>/dev/null
    fi
}
init_log_dir

# 统一日志记录器
log() {
    local log_dir
    log_dir=$(dirname "$LOG_FILE")
    # 检查日志目录是否具有可写权限
    if [ -w "$log_dir" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    fi
}

# 格式化消息输出函数
success_msg() { echo -e "${GREEN}✔ $1${NC}"; log "成功: $1"; }
error_msg()   { echo -e "${RED}✘ $1${NC}"; log "错误: $1"; }
warn_msg()    { echo -e "${YELLOW}⚠ $1${NC}"; log "警告: $1"; }
info_msg()    { echo -e "${CYAN}ℹ $1${NC}"; }
title_msg()   { echo -e "\n${BOLD}${PURPLE}>>> $1 <<<${NC}\n"; }

# 依赖检查：验证 Git 是否已安装
check_git() {
    # command -v 是检测命令是否存在的标准 POSIX 写法
    if ! command -v git > /dev/null 2>&1; then
        error_msg "致命错误: 未检测到 Git 环境，请先安装 Git。"
        exit 1
    fi
}

# 环境检查：验证当前是否处于 Git 仓库工作区内 (支持子目录识别)
check_git_repo() {
    # 现代标准写法：无论在仓库的哪个子目录，只要受 git 管理都会返回 true
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

# ================= 业务功能模块 (原子化分离) =================

# 1. 单独执行：追踪与暂存 (Git Add)
do_add() {
    title_msg "📝 步骤 1/3: 暂存文件 (Git Add)"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库，请先初始化"
        return 1
    fi

    # 获取工作区变动情况
    local changes
    changes=$(git status --porcelain)
    if [ -z "$changes" ]; then
        warn_msg "工作区纯净，没有需要暂存的修改文件。"
        return 0
    fi

    echo -e "${YELLOW}待暂存的变更文件:${NC}"
    git status --short
    echo ""

    info_msg "正在执行文件追踪 (git add .) ..."
    git add .
    
    # 单独校验执行结果
    if [ $? -eq 0 ]; then
        success_msg "所有变更已成功加入暂存区！"
        return 0
    else
        error_msg "暂存失败，请检查文件权限。"
        return 1
    fi
}

# 2. 单独执行：生成快照 (Git Commit)
do_commit() {
    title_msg "📦 步骤 2/3: 提交快照 (Git Commit)"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库，请先初始化"
        return 1
    fi

    # 检查暂存区是否有待提交的内容
    local staged_changes
    staged_changes=$(git diff --cached --name-only)
    if [ -z "$staged_changes" ]; then
        warn_msg "暂存区为空。请先执行 [1] 暂存文件 (Add) 再进行提交。"
        return 0
    fi

    # 捕获用户自定义提交信息
    read -p "请输入提交信息 (直接回车默认: Update Up): " msg
    if [ -z "$msg" ]; then
        msg="Update Up"
    fi

    info_msg "正在生成本地提交快照..."
    git commit -m "$msg"
    
    if [ $? -eq 0 ]; then
        success_msg "版本快照生成完毕！"
        return 0
    else
        error_msg "提交失败，请检查配置或终端输出。"
        return 1
    fi
}

# 3. 单独执行：推送云端 (Git Push)
do_push() {
    title_msg "🚀 步骤 3/3: 推送至云端 (Git Push)"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi

    # 获取当前所在分支 (现代命令 --show-current)
    local curr
    curr=$(git branch --show-current)
    if [ -z "$curr" ]; then
        curr="main"
    fi

    info_msg "正在推送数据包至 origin/$curr ..."
    git push origin "$curr"

    # 根据状态码判断推送是否成功
    if [ $? -eq 0 ]; then
        success_msg "代码已成功同步至云端！"
        return 0
    else
        warn_msg "常规推送被拒绝。远程仓库可能包含您本地没有的更改。"
        read -p "⚠ 是否执行安全强制推送 (使用 --force-with-lease 避免误覆盖他人代码)? (y/n): " force_push
        if [[ "$force_push" =~ ^[Yy]$ ]]; then
            info_msg "启动安全覆盖协议 (git push --force-with-lease) ..."
            git push --force-with-lease --set-upstream origin "$curr"
            
            if [ $? -eq 0 ]; then
                success_msg "安全强推成功！远程状态已被本地更新覆盖。"
                return 0
            else
                error_msg "强推失败，可能存在更严重的冲突或网络问题。"
                return 1
            fi
        else
            info_msg "操作已取消。建议先执行拉取操作。"
            return 1
        fi
    fi
}

# 4. 单独执行：拉取更新 (Git Pull)
do_pull() {
    title_msg "📥 拉取最新更新 (Git Pull)"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi

    local curr
    curr=$(git branch --show-current)
    if [ -z "$curr" ]; then
        curr="main"
    fi

    info_msg "1/2 探测远程状态 (git fetch)..."
    git fetch origin 2>/dev/null

    # 冲突阻断机制
    local local_changes
    local_changes=$(git status --porcelain)
    local stash_choice="n"
    
    if [ -n "$local_changes" ]; then
        warn_msg "检测到本地有未提交的更改。直接拉取可能导致冲突！"
        read -p "是否先暂存(stash)本地更改，安全拉取后再恢复? (y/n): " stash_choice
        if [[ "$stash_choice" =~ ^[Yy]$ ]]; then
            git stash
            info_msg "本地更改已存入 stash。"
        fi
    fi

    info_msg "2/2 下载与合并 (git pull origin $curr)..."
    git pull origin "$curr"
    
    if [ $? -eq 0 ]; then
        success_msg "拉取成功，本地已是最新版本。"
        if [[ "$stash_choice" =~ ^[Yy]$ ]]; then
            warn_msg "请记得手动执行 'git stash pop' 来恢复您刚才暂存的本地代码。"
        fi
        return 0
    else
        error_msg "拉取过程产生冲突或网络连接失败。"
        return 1
    fi
}

# 5. 现代版：分支管理 (使用 git switch)
manage_branches() {
    title_msg "🌿 分支管理 (Branch)"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi
    
    echo -e "${CYAN}当前分支列表:${NC}"
    git branch -a
    echo ""
    echo "1) 基于当前状态创建并切换至新分支"
    echo "2) 切换到已存在的其他分支"
    echo "3) 返回主菜单"
    read -p "请选择分支指令编号: " b_choice
    
    case $b_choice in
        1) 
            read -p "请输入新分支名称 (无空格): " b_name
            if [ -n "$b_name" ]; then
                git switch -c "$b_name"
                if [ $? -eq 0 ]; then
                    success_msg "已创建并切换至新分支: $b_name"
                    return 0
                else
                    error_msg "分支创建失败"
                    return 1
                fi
            fi
            ;;
        2) 
            read -p "请输入目标分支名称: " b_name
            if [ -n "$b_name" ]; then
                git switch "$b_name"
                if [ $? -eq 0 ]; then
                    success_msg "成功切换至分支: $b_name"
                    return 0
                else
                    error_msg "分支切换失败"
                    return 1
                fi
            fi
            ;;
        3) 
            info_msg "操作取消"
            return 0
            ;;
        *)
            error_msg "无效选项"
            return 1
            ;;
    esac
}

# 6. 单独执行：绑定远程地址
bind_remote() {
    title_msg "🔗 绑定远程仓库地址"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi
    
    local current_url
    current_url=$(git remote get-url origin 2>/dev/null)
    
    echo -e "当前设备识别到的源地址: ${YELLOW}${current_url:-"[本地暂无配置远程源]"}${NC}"
    echo -e "脚本预设的目标源地址:   ${GREEN}$MY_REPO_URL${NC}"
    
    read -p "确认将本地仓库指向预设目标地址吗? (y/n): " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        git remote remove origin 2>/dev/null
        git remote add origin "$MY_REPO_URL"
        
        if [ $? -eq 0 ]; then
            success_msg "远程源绑定成功！"
            return 0
        else
            error_msg "绑定失败，请检查权限。"
            return 1
        fi
    fi
    return 0
}

# 7. 现代版：初始化仓库
init_repo() {
    title_msg "📦 初始化 Git 仓库"
    if check_git_repo; then 
        error_msg "阻止操作：当前已经是受控 Git 仓库"
        return 1
    fi
    
    git init --initial-branch=main
    
    if [ $? -eq 0 ]; then
        success_msg "仓库初始化完毕！默认主分支已设为: main"
        return 0
    else
        error_msg "初始化失败"
        return 1
    fi
}

# 8. 单独执行：历史查询
view_logs() {
    title_msg "📜 提交历史溯源"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi
    git --no-pager log --graph --pretty=format:'%Cred%h%Creset -%C(yellow)%d%Creset %s %Cgreen(%cr) %C(bold blue)<%an>%Creset' --abbrev-commit -n 15
    echo -e "\n"
    return 0
}

# 9. 状态剖析与明细
view_status() {
    title_msg "📊 库区状态剖析"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi

    echo -e "${CYAN}【当前文件级状态概览 (git status -s)】${NC}"
    git status -s
    echo ""

    echo -e "${CYAN}【工作区尚未暂存的代码变动 (git diff)】${NC}"
    git --no-pager diff
    echo ""

    echo -e "${CYAN}【已放入暂存区待提交的代码变动 (git diff --cached)】${NC}"
    git --no-pager diff --cached
    echo ""
    return 0
}

# 10. 系统操作：切换目录
change_dir() {
    title_msg "📁 切换物理工作目录"
    echo -e "当前系统位置: ${YELLOW}$(pwd)${NC}"
    read -p "请输入新路径 (绝对/相对均可): " new_path
    if [ -n "$new_path" ]; then
        if [ ! -d "$new_path" ]; then
            mkdir -p "$new_path" 2>/dev/null
        fi
        
        cd "$new_path" || { error_msg "无法进入指定路径"; return 1; }
        
        if [ "$(pwd)" = "$new_path" ] || [ "$(pwd)" = "$(realpath "$new_path" 2>/dev/null)" ]; then
            success_msg "系统位置已成功转移至: $(pwd)"
            return 0
        fi
    fi
    return 0
}

# 11. 系统操作：深度清理
deep_clean() {
    title_msg "🧹 垃圾回收与深度清理"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi
    
    info_msg "清理历史动作残留并压缩数据库..."
    git reflog expire --expire=now --all 2>/dev/null
    git gc --prune=now --aggressive 2>/dev/null
    
    if [ $? -eq 0 ]; then
        success_msg "清理成功！当前 .git 体积为: $(du -sh .git 2>/dev/null | cut -f1)"
        return 0
    else
        error_msg "清理任务中断或失败。"
        return 1
    fi
}

# 12. 单独执行：恢复暂存代码 (Stash Pop)
restore_stash() {
    title_msg "📦 恢复暂存代码 (Git Stash Pop)"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi

    local stash_count
    stash_count=$(git stash list | wc -l)
    
    if [ "$stash_count" -eq 0 ]; then
        warn_msg "当前没有发现任何被暂存 (stash) 的代码记录。"
        return 0
    fi

    echo -e "${CYAN}【当前的暂存记录列表 (git stash list)】${NC}"
    git --no-pager stash list
    echo ""

    local local_changes
    local_changes=$(git status --porcelain)
    if [ -n "$local_changes" ]; then
        warn_msg "高危拦截：您的工作区目前存在未提交的修改！"
        error_msg "此时强制释放暂存极大概率会导致覆盖报错 (Aborting)。"
        info_msg "建议方案：请先按 [1] 暂存并 [2] 提交当前代码，然后再执行此操作。"
        read -p "⚠ 是否仍要无视警告强行尝试释放？(y/n): " force_pop
        if [[ ! "$force_pop" =~ ^[Yy]$ ]]; then
            info_msg "操作已安全取消。"
            return 0
        fi
    fi

    read -p "检测到有 $stash_count 条暂存记录，是否立即恢复最新的一条并合并回工作区? (y/n): " pop_choice
    if [[ "$pop_choice" =~ ^[Yy]$ ]]; then
        info_msg "正在释放暂存区代码 (git stash pop)..."
        
        local pop_output
        pop_output=$(git stash pop 2>&1)
        local pop_status=$?

        echo -e "${CYAN}$pop_output${NC}"

        if [ $pop_status -eq 0 ]; then
            success_msg "恢复成功！您暂存的代码已安全回到工作区。"
            return 0
        elif echo "$pop_output" | grep -q "Aborting"; then
            error_msg "恢复被 Git 中止！原因：工作区存在冲突的未保存文件。请先提交或丢弃当前更改。"
            return 1
        else
            error_msg "恢复时产生合并冲突！请打开编辑器解决文件内的冲突标记 (<<<<<<<) 后再提交。"
            return 1
        fi
    else
        info_msg "已取消操作。您的代码依然安全地保留在 stash 中。"
        return 0
    fi
}

# 13. 新增：一键自动化工作流 (Pull -> Stash Pop -> Add -> Commit -> Push)
do_one_click() {
    title_msg "⚡ 执行一键同步工作流"
    if ! check_git_repo; then 
        error_msg "当前目录非 Git 仓库"
        return 1
    fi

    local curr
    curr=$(git branch --show-current 2>/dev/null)
    if [ -z "$curr" ]; then curr="main"; fi

    # 1. 尝试拉取 (Pull)
    info_msg "[1/5] 正在拉取远程更新 (Git Pull)..."
    local pull_out
    # 捕获原生输出和错误
    pull_out=$(git pull origin "$curr" 2>&1)
    if [ $? -ne 0 ]; then
        error_msg "拉取失败！Git 报错原因如下："
        echo -e "${RED}$pull_out${NC}"
        return 1
    fi

    # 2. 尝试恢复暂存 (Stash Pop)
    local stash_count
    stash_count=$(git stash list | wc -l)
    if [ "$stash_count" -gt 0 ]; then
        info_msg "[2/5] 发现暂存记录，正在恢复 (Git Stash Pop)..."
        local pop_out
        pop_out=$(git stash pop 2>&1)
        if [ $? -ne 0 ]; then
            error_msg "恢复暂存失败！Git 报错原因如下："
            echo -e "${RED}$pop_out${NC}"
            return 1
        fi
    else
        info_msg "[2/5] 无暂存记录，自动跳过恢复。"
    fi

    # 3. 暂存所有变更 (Add)
    info_msg "[3/5] 正在追踪变动文件 (Git Add)..."
    local add_out
    add_out=$(git add . 2>&1)
    if [ $? -ne 0 ]; then
        error_msg "暂存文件失败！Git 报错原因如下："
        echo -e "${RED}$add_out${NC}"
        return 1
    fi

    # 4. 提交快照 (Commit)
    info_msg "[4/5] 正在生成快照 (Git Commit)..."
    # 如果有可提交的改动才执行 commit
    if ! git diff --cached --quiet; then
        local commit_out
        # 自动化提交，附带时间戳
        commit_out=$(git commit -m "Update Up" 2>&1)
        if [ $? -ne 0 ]; then
            error_msg "提交代码失败！Git 报错原因如下："
            echo -e "${RED}$commit_out${NC}"
            return 1
        fi
    else
        warn_msg "工作区没有需要提交的新变动，继续下一步。"
    fi

    # 5. 推送至云端 (Push)
    info_msg "[5/5] 正在推送至云端 (Git Push)..."
    local push_out
    push_out=$(git push origin "$curr" 2>&1)
    if [ $? -ne 0 ]; then
        error_msg "推送失败！Git 报错原因如下："
        echo -e "${RED}$push_out${NC}"
        return 1
    fi

    success_msg "🎉 一键同步工作流全部执行成功！"
    return 0
}

# ================= 终端前端 GUI / 菜单仪表盘 =================
show_dashboard() {
    clear 2>/dev/null || printf '\033[2J\033[H'
    echo -e "${BOLD}${BLUE}══════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${CYAN}          🛠️ Git Master 控制台 (原子重构版)      ${NC}"
    echo -e "${BOLD}${BLUE}══════════════════════════════════════════════${NC}"
    
    echo -e " 📍 ${BOLD}物理坐标:${NC} ${YELLOW}$(pwd)${NC}"
    
    if check_git_repo; then
        local b_name
        b_name=$(git branch --show-current 2>/dev/null)
        local changes
        changes=$(git status --porcelain 2>/dev/null | wc -l)
        local remote
        remote=$(git remote get-url origin 2>/dev/null || echo "未绑定远程")
        
        echo -e " 🌿 ${BOLD}当前分支:${NC} ${GREEN}${b_name:-"游离状态/未命名"}${NC}"
        echo -e " 🔗 ${BOLD}远程目标:${NC} ${CYAN}${remote}${NC}"
        
        if [ "$changes" -gt 0 ]; then
            echo -e " 📝 ${BOLD}变动预警:${NC} ${RED}工作区有 $changes 个变更未处理${NC}"
        else
            echo -e " 📝 ${BOLD}变动预警:${NC} ${GREEN}工作区完全纯净${NC}"
        fi
    else
        echo -e " ⚠️  ${BOLD}存储核心:${NC} ${RED}未检测到 Git 数据库${NC}"
    fi
    echo -e "${BOLD}${BLUE}──────────────────────────────────────────────${NC}"
    
    echo -e " ${YELLOW}[1] 📝 暂存变动 (Git Add)${NC}"
    echo -e " ${GREEN}[2] 📦 创建快照 (Git Commit)${NC}"
    echo -e " ${CYAN}[3] 🚀 推送云端 (Git Push)${NC}"
    echo -e " ${PURPLE}[4] 📥 拉取更新 (Git Pull)${NC}"
    echo -e " ${BLUE}[5] 🌿 分支管理 (Branch)${NC}"
    echo -e " ${YELLOW}[6] 📊 状态明细 (Status & Diff)${NC}"
    echo -e " ${CYAN}[7] 📜 历史查询 (Log Graph)${NC}"
    echo -e " ${PURPLE}[8] 🔗 绑定源址 (Bind Remote)${NC}"
    echo -e " ${GREEN}[9] 📦 建立仓库 (Init Repo)${NC}"
    echo -e " ${YELLOW}[10] 📁 切换目录 (Change Dir)${NC}"
    echo -e " ${RED}[11] 🧹 深度清理 (Git GC)${NC}"
    echo -e " ${PURPLE}[12] 📦 恢复暂存 (Stash Pop)${NC}"
    echo -e " ${BOLD}${YELLOW}[13] ⚡ 一键同步 (Pull>Pop>Add>Commit>Push)${NC}"
    echo -e " ${BOLD}[0] ❌ 退出终端 (Exit)${NC}"
    echo -e "${BOLD}${BLUE}══════════════════════════════════════════════${NC}"
}

# ================= 权限前置防线 =================
if [ "$(id -u)" -ne 0 ]; then
    warn_msg "环境提示：未检测到 Root 权限，针对根目录等高权区域可能会读写受阻。"
fi

check_git

# ================= 命令行外置参数解析路由器 =================
if [ $# -gt 0 ]; then
    case "$1" in
        add)    do_add ;;
        commit) do_commit ;;
        push)   do_push ;;
        pull)   do_pull ;;
        branch) manage_branches ;;
        status) view_status ;;
        log)    view_logs ;;
        bind)   bind_remote ;;
        init)   init_repo ;;
        cd)     change_dir ;;
        clean)  deep_clean ;;
        stash)  restore_stash ;;
        sync)   do_one_click ;;
        help|-h|--help)
            echo -e "${CYAN}Git Master CLI 独立模式使用指南:${NC}"
            echo -e "  add    : 暂存当前所有改动"
            echo -e "  commit : 为暂存的内容创建快照"
            echo -e "  push   : 将本地提交推送到远程仓库"
            echo -e "  sync   : 执行一键同步"
            echo -e "  ...其他命令见菜单"
            ;;
        *) error_msg "未识别的参数: $1" ;;
    esac
    exit 0
fi

# ================= 交互式生命周期循环 =================
while true; do
    show_dashboard
    read -p "👉 键入数字并回车: " choice
    
    # 标记是否需要等待用户按下回车键 (默认需要)
    require_pause=true
    
    case $choice in
        1) do_add; [ $? -eq 0 ] && require_pause=false ;;
        2) do_commit; [ $? -eq 0 ] && require_pause=false ;;
        3) do_push; [ $? -eq 0 ] && require_pause=false ;;
        4) do_pull; [ $? -eq 0 ] && require_pause=false ;;
        5) manage_branches ;; # 分支和查看类信息保持暂停，避免文字闪过
        6) view_status ;;
        7) view_logs ;;
        8) bind_remote; [ $? -eq 0 ] && require_pause=false ;;
        9) init_repo; [ $? -eq 0 ] && require_pause=false ;;
        10) change_dir; [ $? -eq 0 ] && require_pause=false ;;
        11) deep_clean; [ $? -eq 0 ] && require_pause=false ;;
        12) restore_stash; [ $? -eq 0 ] && require_pause=false ;;
        13) do_one_click; [ $? -eq 0 ] && require_pause=false ;;
        0) echo "控制台已下线。"; exit 0 ;;
        *) error_msg "非法的选项指令，请确认您输入的数字有效" ;;
    esac
    
    echo ""
    if [ "$require_pause" = true ]; then
        # 失败、或者执行查看类操作时，要求按回车以便查阅终端内容
        read -p "按 [Enter] 键继续..."
    else
        # 成功完成操作时，停留一小会儿让用户看清成功提示，然后自动刷新面板
        info_msg "操作成功完成，即将自动返回主菜单..."
        sleep 1.5 
    fi
done
