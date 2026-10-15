# 发布排版

- 中文使用 **Microsoft YaHei / 微软雅黑**；英文、数字和单位使用 **Times New Roman / 新罗马**。
- 重点用粗体和蓝色 #1F4E79；适用范围用棕色 #8A5700，未经验证的结果不用验收通过色。
- GitHub Markdown 不能强制字体。完整排版见 `docs/site/` 本地 HTML；字体依赖系统安装，不随包分发。
- 复用 wmw-abaqus 的 publication.css 与中英文拆分规则，代码文本不改写。

在仓库根目录重建：

```powershell
python -m pip install "mistune>=3,<4"
python scripts/build_publication.py
```

推送前检查实际字体、粗体/颜色、标题裁切、本地链接和命令复制。记录见 [validation.md](validation.md)。
