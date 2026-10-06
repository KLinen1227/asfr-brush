# 下载文件与安装包结构

## 下载文件

在项目的 **Releases → Assets** 中选择对应文件：

| 文件 | 用途 |
| --- | --- |
| `ASFR-Brush-0.0.1.zip` | ASFR笔刷0.0.1 的 Blender 安装包 |
| `ASFR-Brush-0.0.1.sha256.txt` | 安装包的 SHA256 校验值 |
| `Source code (zip)` / `Source code (tar.gz)` | 项目源码归档，不用于直接安装插件 |

## 安装包结构

安装包顶层为 `petrify_painter/` 文件夹，其中包含 `__init__.py`、功能模块、默认材质节点库和使用说明书。

安装时直接选择 ZIP，无需解压。操作步骤见 [使用说明书](USER_GUIDE.md)。

## 文件校验

Windows 用户可在安装包所在目录打开 PowerShell，运行：

```powershell
Get-FileHash -LiteralPath '.\ASFR-Brush-0.0.1.zip' -Algorithm SHA256
```

将结果与 `ASFR-Brush-0.0.1.sha256.txt` 中的校验值比较。校验值不一致时，请重新下载文件后再安装。
