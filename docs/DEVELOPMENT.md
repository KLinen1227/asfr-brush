# 开发与本地构建

插件模块名保持 `petrify_painter`，避免仅改展示名称就破坏旧工程兼容。当前版本是传统 Blender add-on，不含 Extensions manifest。

## 文件分工

- `__init__.py`：设置、操作器、笔刷、路径、预设管理与面板。
- `petrify_overlay.py`：绘制范围叠加预览。
- `petrify_preview.py`：轻量材质预览。
- `petrify_topology.py`：固定最终拓扑下的属性存储与传递。
- `petrify_management.py`：预设参数记忆及批量暂存/回滚。
- `petrify_resin.py`、`petrify_ice.py`、`petrify_gold.py`：各效果预设。
- `petrify_normals.py`：人物法线与材质细节混合。

## 不启动 Blender 的检查

在项目根目录，用 Python 3.11 或更新版本运行：

```sh
python tools/verify_public.py
python -m unittest discover -s tests -v
python tools/build_release.py
```

检查器验证当前公开版的精确文件白名单、SHA256、版本及 PUBLIC 标识，并检查明显的凭证/个人路径模式。扫描不是完整安全审计。
构建器只从白名单选择插件文件，生成 `dist/ASFR笔刷0.0.1.zip` 和 SHA256 文件；不递归打包项目或上级工作目录。
相同 Python/zlib 环境中重复构建的 ZIP 一致。重建 ZIP 的压缩元数据与原发布包可能不同，因此整个 ZIP 的 SHA256 可以不同，但插件内每个文件必须与审核基线一致。两种 ZIP 应各用自己的校验文件，不能混用。
当前基线固定于 0.0.1；将来改代码、许可证、署名或增加资源时，应人工复核并更新基线和白名单，而不是绕过检查。

## Blender 冒烟测试

请只在独立进程使用默认场景测试，不对正在工作的工程运行测试脚本。

```sh
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tools/blender_smoke.py
```

测试使用默认立方体和小尺寸蒙版，验证四款预设、参数记忆、时间路径启用/独显/擦除/删除及注册注销。它还检查配布材质库的实际数据块清单。没有人体模型或外部贴图。
这只是冒烟测试，不替代 GUI 笔刷交互、撤销重做、渲染与复杂工程测试。

## 不执行发布

工具没有登录 GitHub、推送代码或创建 Release 的功能，也没有自动发布工作流。`--publish-check` 仅检查发布前待确认事项，不会发送文件。
它不代替授权来源审查；没有真实 LICENSE 或尚未明确许可、资源权利及上传授权时，检查会按设计返回非零退出码。
