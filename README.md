# CNC G 代码生成工具

> 把 AutoCAD 画的 DXF / DWF / DWFx 文件（多段线 / 直线 / 圆弧 / 圆 / 样条曲线）
> 转换成 CNC 雕刻机能用的 G 代码。
> 输出严格限定为 `G00 / G01 / G02 / G03` 四种指令，无宏程序、无固定循环。

---

## 📁 项目结构

```
d:\Desktop\G-code-generation-tool\
├── README.md                ← 你正在读的文件
├── convert.bat              ← 一键转换（拖文件上去就转）
├── preview.bat              ← 一键预览（自动生成两张图）
├── dxf2gcode.py             ← 核心转换脚本
│
├── preview\
    └── preview_gcode.py     ← G 代码预览脚本（Pillow 画 PNG）
```

---

## ⚡ 快速开始（30 秒）

### 1. 准备 CAD 文件

支持三种格式（脚本自动识别扩展名）：

| 格式 | 扩展名 | 所需依赖 |
|------|--------|----------|
| **DXF** | `.dxf` | ezdxf（必装） |
| **DWF** | `.dwf` | ezdwf（可选，DWF 才需要） |
| **DWFx** | `.dwfx` | ezdwf（可选，DWFx 才需要） |

**内容要求：**
- 线条类型：`LWPOLYLINE / POLYLINE / LINE / ARC / CIRCLE / SPLINE`
- 坐标原点在图纸**右下角**（X 向左负方向，Y 向上正方向）
- 可以画一个最大外框矩形，工具会自动跳过它

### 2. 安装依赖（只需执行一次）

```powershell
# 必装：ezdxf + Pillow
"C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe" -m pip install ezdxf pillow

# 可选：如果要读 DWF / DWFx 文件再加这个
"C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe" -m pip install ezdwf
```

### 3. 转换

**方式 A（最简单）：** 把文件拖到 `convert.bat` 上

**方式 B（PowerShell 命令行）：**
```powershell
cd d:\Desktop\G-code-generation-tool

# DXF
.\convert.bat 01.dxf

# DWF（新增支持！）
.\convert.bat 02.dwf
```

**方式 C（直接调用 Python）：**
```powershell
python dxf2gcode.py -i 02.dwf -o 02.txt
```

成功输出示例：
```
【文件格式】DWF
【DXF 原始范围】X: 99.954 ~ 147.325  (47.371 mm)
               Y: 38.929 ~ 76.076  (37.148 mm)
【加工原点位置】br（CNC (0,0) = DXF (147.325, 38.929)）
【平移后 CNC 范围】X: -47.371 ~ 0.000
                 Y: 0.000 ~ 37.148
【完成】共处理 23 个图元，跳过边框 1 个
【输出】G 代码已保存至：02.txt
```

### 4. 预览轨迹（强烈建议每次转换后都做）

拖 `.txt`（或 `.nc`）文件到 `preview.bat` 上，或：
```powershell
.\preview.bat 02.txt
```

**自动生成两张图：**

| 文件 | 内容 |
|------|------|
| `02_full.png` | **完整轨迹**：蓝色切削 + 灰色快速 G00 + 浅灰抬刀空跑 |
| `02_cutting.png` | **纯切削轨迹**：只有蓝色雕刻形状，一眼看清最终效果 |

---

## 📝 输出 G 代码格式

### 程序头（固定）
```
O1099;              ← 程序号（可自定义（诶嘿））
M03S3000;           ← 主轴正转 3000 RPM
G54G90;             ← 工件坐标系 + 绝对坐标
G00Z15;             ← 快速抬刀到安全高度
G00X0Y0;            ← 快速回原点
```

### 每段轮廓
```
G00 Z15.000          ← 抬刀（模态保持，安全高）
G00 X-33.803 Y17.737 ← 快速移动到起点上方
G01 Z-0.200 F200     ← 下刀切入（F 模态保持）
G01 X-22.649 Y17.737 ← 切削直线
G01 X-22.649 Y8.826
...
G00 Z15.000          ← 抬刀，准备下一段
```

### 程序尾
```
G00 Z15.000          ← 确保在安全高
M30                  ← 程序结束，回到开头
```

### 坐标规则

| 字段 | 格式 | 说明 |
|------|------|------|
| X / Y | 保留 3 位小数 | 绝对坐标（G90 模态） |
| Z | 保留 3 位小数 | 安全高 +15，下刀 -0.2 |
| I / J | 保留 3 位小数 | G02/G03 专用，圆心相对起点的偏移 |
| F | 整数 | 每分钟进给，模态保持，默认 200 |

### 支持的 G 指令

| 指令 | 用途 | 说明 |
|------|------|------|
| G00 | 快速定位 | 抬刀状态下用 |
| G01 | 直线插补 | 切削 / 空跑 |
| G02 | 顺时针圆弧 | 带 I、J 圆心偏移 |
| G03 | 逆时针圆弧 | 带 I、J 圆心偏移 |

**不会出现：** G41（刀补）、G80-G89（固定循环）、G98/G99、M98/M99（子程序）、宏程序变量等。

---

## ⚙️ 参数说明

### 命令行参数（Python 脚本）

| 参数 | 全称 | 默认值 | 含义 |
|------|------|--------|------|
| `-i` | `--input` | **必填** | 输入文件路径（.dxf / .dwf / .dwfx） |
| `-o` | `--output` | **必填** | 输出 G 代码文件路径（建议 .txt） |
| `-s` | `--safe-z` | `15.0` | 安全高度 Z（抬刀高度，mm） |
| `-d` | `--cut-depth` | `-0.2` | 下刀深度（负值，mm） |
| `-f` | `--feed` | `200` | 进给速度 F（mm/min） |
| `-p` | `--origin` | `br` | 加工原点位置：`bl`左下 / `br`右下 / `tl`左上 / `tr`右上 |

### 举例

```powershell
# 最简（全部默认参数）
python dxf2gcode.py -i 02.dwf -o 02.txt

# 自定义原点 + 下刀更深 + 更快
python dxf2gcode.py -i 02.dwf -o 02.txt -p bl -d -0.5 -f 300

# 原点在左上 + 安全高 20mm
python dxf2gcode.py -i 02.dwf -o 02.txt -p tl -s 20
```

---

## 🛠️ 外框怎么处理？

工具会**自动识别并跳过**覆盖整个图形最大范围的外框线：
- LINE 水平覆盖全宽 **或** 垂直覆盖全高 → 外框边
- POLYLINE 包围盒恰好等于整体包围盒 → 闭合外框

你可以放心在 CAD 里画外框，转换时会自动消失。

---

## 🐛 常见问题

### Q: 提示 "ModuleNotFoundError: No module named 'ezdxf'"
A: 默认 Python 路径不对。用 bat 或显式指定 Python 3.10 路径。
   手动装：`python3.10 -m pip install ezdxf pillow`

### Q: DWF 文件报错 "需要 ezdwf 库"
A: DWF 格式需要额外装 ezdwf：`python3.10 -m pip install ezdwf`

### Q: 为什么输出里没有 G02/G03？
A: 输入里没有 ARC 或带 bulge 的多段线。DWG 转换时曲线被打散成小直线了。
   解决：用 AutoCAD 直接保存为高版本 DWF 或 DXF（不要用第三方转换器）。

### Q: 边框没被跳过？
A: 检查边框是不是恰好覆盖整个图形的 X 或 Y 范围。有圆角/偏移则识别不出。
   临时解决：在 CAD 里把边框改回严格矩形再保存。

### Q: 加工出来的形状偏小 0.3mm？
A: 刀直径 0.3mm，走刀中心轨迹自然会偏小。要么加 G41 半径补偿，
   要么在 CAD 里提前把轮廓向外偏移 0.15mm。

### Q: 转换的 .txt 拷到机床里能识别吗？
A: 绝大多数 CNC 控制器（FANUC / 广数 / 华中）**不关心后缀名**，
   只认文件里的 G 代码内容。如果你的机床要求特定后缀，改回去即可。

---

## 🔧 技术栈

- **Python** 3.10（路径：`C:\Users\ASUS\AppData\Local\Programs\Python\Python310\`）
- **ezdxf** 1.4+ （DXF 解析，必装）
- **ezdwf** 0.0.6+ （DWF / DWFx 解析，可选）
- **Pillow** （PNG 预览绘图）
- Windows 10 / 11

---

## 📄 许可

本项目仅用于个人 CNC 雕刻场景，可自由修改和分发。
