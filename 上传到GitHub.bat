@echo off
chcp 65001 >nul
title 上传到 GitHub - douyin-spark-xiaogui

echo ========================================
echo   Douyin Spark Keeper - 上传到 GitHub
echo ========================================
echo.

:: 进入脚本所在目录（项目目录）
cd /d "%~dp0"

:: 检查 git 是否安装
where git >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 git 命令。
    echo.
    echo 请先安装 Git for Windows：https://git-scm.com/download/win
    echo 安装完成后，重新双击运行本脚本。
    echo.
    pause
    exit /b 1
)

echo [1/6] 初始化本地仓库...
git init

echo [2/6] 添加所有文件...
git add .

echo [3/6] 提交代码...
git commit -m "feat: 支持多账号登录态上传与分时发送" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 没有新内容需要提交，可能已提交过。
)

set REMOTE_URL=https://github.com/gaoshibo56-gif/douyin-spark-xiaogui.git

echo [4/6] 关联远程仓库...
echo   %REMOTE_URL%
git remote remove origin >nul 2>&1
git remote add origin %REMOTE_URL%

echo [5/6] 设置分支...
git branch -M main

echo [6/6] 推送到 GitHub...
git push -u origin main

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo   上传成功！
    echo   仓库地址：%REMOTE_URL%
    echo   浏览器打开：https://github.com/gaoshibo56-gif/douyin-spark-xiaogui
    echo ========================================
) else (
    echo.
    echo [错误] 推送失败，常见原因及解决办法：
    echo.
    echo   1. 远程仓库尚未创建
    echo      -> 请先登录 GitHub，访问 https://github.com/new 新建一个名为 douyin-spark-xiaogui 的空仓库
    echo      -> 切记：不要勾选 "Add a README file"，直接点 Create repository
    echo.
    echo   2. 需要登录验证
    echo      -> 推送时若弹出窗口，请输入你的 GitHub 账号名和 Personal Access Token（不是登录密码）
    echo      -> 如何创建 Token：GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens
    echo.
    echo   3. 远程仓库已有内容导致冲突
    echo      -> 可尝试先执行：git pull --rebase origin main
    echo      -> 然后再运行本脚本
)

pause
