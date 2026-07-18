# AutoCover

AutoCover 是统一自动化项目中的本地封面工作台。它从成品切片中抽取候选帧，避开片头、片尾和高风险字幕区域，并分别生成 `4:3` 与 `16:9` 封面。

通常不需要单独启动它。在项目根目录执行：

```powershell
python 启动.py
```

启动器会自动启动 AutoSlice 和 AutoCover。需要单独调试时，在本目录执行：

```powershell
python -m autocover.cli serve
```

## 使用流程

1. 载入自动切片或精调后的成品视频目录。
2. 导入投稿标题文档，或直接编辑标题。
3. 从候选帧中选择画面。
4. 在预览区拖动、缩放标题和贴图，调整颜色、描边及画面焦点。
5. 单独导出当前比例，或保存 `4:3` 和 `16:9` 双比例封面。

濑户体不是仓库内的可再分发资源。可以通过 `AUTOCOVER_FONT_PATH` 指向本机合法取得的字体文件。贴图目录通过 `AUTOCOVER_STICKER_DIR` 指定，默认使用项目内的 `stickers` 目录。

## 独立批处理

```powershell
python -m autocover.cli batch "output" --output-dir "covers" --canvas both
```

AutoCover 不调用外部 AI 服务，也不会上传视频、标题或图片。
