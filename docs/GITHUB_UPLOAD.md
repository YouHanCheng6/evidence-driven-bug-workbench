# 上传到个人 GitHub

以下步骤只适用于 `my/` 这个无历史的个人定制脱敏展示版。不要在原工作区根目录执行，也不要上传内部交付包或父目录的 `.git`。阅读 README 的贡献边界与展示限制。

## 上传前

1. 完成 [知识产权与安全门禁](SECURITY_AND_IP_GATE.md)。
2. 在 GitHub 新建一个空仓库，不要自动生成 README、License 或 `.gitignore`。
3. 首次推送先使用 Private 仓库完成最终审查；公司政策不允许外传时，即使 Private 也不要推送。
4. 在终端进入 `my/`，确认当前目录正确：

```bash
pwd
find . -maxdepth 3 -type f -print
```

5. 运行仓库自带的基础检查，再使用组织批准的秘密扫描器扫描当前目录。不要因为扫描无结果就推断公司已授权发布：

```bash
python3.12 scripts/check_public_release.py
python3.12 -m unittest discover -s tests/public_release -v
```

## 创建全新历史

```bash
cd my
git init
git branch -M main
git config user.email "YOUR_GITHUB_NOREPLY_ADDRESS"
git add .
git diff --cached
git commit -m "Initial sanitized personal bug-workbench showcase"
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
git remote -v
git push -u origin main
```

首次推送前必须人工检查 `git diff --cached`。如果远端地址、账号或文件列表不符合预期，停止操作并修正，不要强制推送。

若不希望提交暴露私人邮箱，请在 GitHub 邮箱设置中复制你的 `noreply` 地址，替换上面的占位值；不要把示例占位值直接提交成作者邮箱。

## 后续添加实现

- 每次只从经过授权的独立来源加入文件。
- 凭据只放在本地 `.env` 或 GitHub Actions Secrets 中。
- 提交前检查 `git status --short` 和 `git diff --cached`。
- 一旦误传机密，先撤销凭据并联系公司安全团队；仅删除最新提交并不能清除 Git 历史。
