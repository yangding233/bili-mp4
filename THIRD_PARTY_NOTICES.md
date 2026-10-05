# 第三方组件说明

本项目的 MIT 许可证仅适用于自行编写的应用代码。打包后的第三方组件采用各自许可证，原始版权与许可声明必须保留。

| 组件 | 固定直接版本 | 上游及许可 |
| --- | --- | --- |
| Python | CI 使用 3.11 的可用补丁版本 | [Python 许可](https://docs.python.org/3/license.html)；打包时保存解释器的 LICENSE.txt |
| PySide6 / Qt | 6.11.2 | [PySide6 发布资料](https://pypi.org/project/PySide6/6.11.2/)、[Qt LGPL 文本](https://doc.qt.io/qt-6/lgpl.html)；实际模块许可以随包文件为准 |
| yt-dlp | 2026.8.19 | [源码与许可](https://github.com/yt-dlp/yt-dlp/tree/2026.08.19)；Unlicense，第三方文件保留各自许可 |
| FFmpeg / ffprobe | 默认 Gyan 8.1.2 essentials | [构建发布方](https://www.gyan.dev/ffmpeg/builds/)明确说明其静态构建为 GPLv3 |
| PyInstaller | 6.22.3，仅构建工具及 bootloader | [许可证与打包例外](https://pyinstaller.org/en/stable/license.html) |
| pytest | 9.1.1，仅测试 | [源码与 MIT 许可](https://github.com/pytest-dev/pytest) |

## 构建时生成的记录

`scripts/collect_licenses.py` 从构建环境的已安装发行包收集 LICENSE、COPYING、NOTICE 等文件到便携包 `licenses/`，并生成包含版本、元数据和复制文件清单的 `dependency-inventory.json`。清单也可能包含只用于构建或测试的包，不表示所有安装包都进入了成品。

FFmpeg 的原始 LICENSE/readme、版本和配置另存于 `licenses/FFmpeg/`。Windows 文件夹包保留 Qt 共享库；本项目不增加禁止调试修改后库或重新构建的条款。

这些记录帮助核对依赖，不替代对实际打包文件与对应源码的核对。生成正式 Release 前，应按实际使用的共享库和静态工具补齐适用许可要求的材料。

## 固定 FFmpeg 下载与来源

默认获取脚本只接受以下固定发布资产：

- 二进制：[ffmpeg-8.1.2-essentials_build.zip](https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/ffmpeg-8.1.2-essentials_build.zip)。
- 发布方资产摘要：[8.1.2 assets](https://github.com/GyanD/codexffmpeg/releases/expanded_assets/8.1.2)。
- SHA256：`db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec`。
- 发布方所指 FFmpeg 源码：[commit 38b88335f9](https://github.com/FFmpeg/FFmpeg/commit/38b88335f9)，[源码归档](https://github.com/FFmpeg/FFmpeg/archive/38b88335f9.tar.gz)。

FFmpeg 作为独立进程运行，应用没有把其库静态链接进自己的代码。**单独的 FFmpeg 上游源码链接并不是该静态构建全部外部库的完整对应源码包。** 正式二进制分发应保存和提供与实际构建匹配的对应源码、外部库版本、补丁及构建材料，并保留许可文本。参考 [FFmpeg 官方许可说明](https://ffmpeg.org/legal.html) 和包内 GPLv3 文本。

如果自行传入其他 FFmpeg 构建，必须传入该构建的原始许可目录和匹配的源码来源。不能用默认 8.1.2 的来源声明替代其他版本的对应材料。

## 更新依赖

升级依赖后重新执行自动化测试、Windows 打包和人工验收，重新生成许可清单及来源记录。不要沿用旧二进制的摘要或源码版本。
