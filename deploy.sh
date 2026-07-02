#!/usr/bin/env bash
# 一键部署:把【本地 main 当前指向的 commit】直推阿里云服务器,触发服务器端 docker 自动构建重启。
#
# 关键语义:本脚本【绝不自动 fetch / pull】GitLab 最新 main,只推你本地 main 此刻的样子。
#   为什么:自动拉最新 main 有时间差风险——别人刚强推/合并的未审内容,会被你顺手部署上去。
#   所以「同步」与「部署」解耦:想更新本地 main 时【单独手动】git pull(或让 Claude 拉),
#   自己 review 没问题后再跑本脚本上线。脚本只负责「把我现在手里的 main 原样发出去」。
#
# 前提:已配好 `deploy` remote(指向服务器裸仓库),且开发机能免密 SSH 到服务器。
set -euo pipefail

# 必须用 Git Bash 运行(不是 WSL 的 bash、也不是 PowerShell)。
# WSL bash 看不到 Windows 的 ~/.ssh/id_rsa → Permission denied;故这里挡一道。
# 注意:不要再 export GIT_SSH_COMMAND —— 覆写会让 git 误判 ssh 变体为 'simple',
# 无法给 GitLab 远端传 -p 2222 端口;Git Bash 默认 ssh 本就支持端口 + id_rsa。
case "$(uname -s)" in
  MINGW* | MSYS*) : ;;
  *)
    echo "✋ 请用 Git Bash 运行本脚本(当前不是 Git Bash)。"
    echo "   开始菜单打开 Git Bash 后:  cd /e/geo && ./deploy.sh"
    echo "   或在 PowerShell 里:        & 'C:\\Program Files\\Git\\usr\\bin\\bash.exe' deploy.sh"
    exit 1
    ;;
esac

# 不 fetch / 不 pull —— 部署的就是你本地 main 现在指向的 commit。先打印出来给你过目。
echo "▶ 即将部署本地 main 的这个 commit:"
git log -1 --oneline main

echo "▶ 推送本地 main → 部署服务器(触发 docker compose up --build -d)..."
git push deploy main:main

echo "✓ 已推送,服务器正在构建。到服务器查看状态/日志:"
echo "    cd ~/geo && docker compose ps && docker compose logs --tail=30 app"
