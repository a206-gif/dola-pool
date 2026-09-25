# Dola Pool - AI视频生成平台

一个基于Dola AI的视频生成平台，支持SD2.0/SD2.5模型，提供API接口、批量生成、账号池管理、代理IP轮换等功能。

## 功能特性

- 🎬 **视频生成** - 支持SD2.0（10秒/15秒）和SD2.5（30秒）模型
- 🔑 **账号池管理** - 多账号轮询，自动分配可用账号
- 🌐 **代理IP轮换** - 支持台湾/香港/新加坡动态住宅IP，自动轮换避免风控
- 📊 **任务管理** - 实时任务状态监控，一键重试失败任务
- 🎨 **节点画布** - 可视化节点式视频创作工作流
- 🤖 **AI推理** - 集成Agnes AI，自动生成电影级分镜提示词
- 📱 **移动端适配** - 完美适配手机端操作
- 💧 **去水印** - 自动去除视频水印
- 🔄 **高并发** - 支持100并发批量生成

## 技术栈

- **后端**: Python + FastAPI + SQLite
- **前端**: 原生HTML/CSS/JavaScript
- **代理**: udealproxy 动态住宅IP
- **AI模型**: Dola AI (Seedance 2.0/2.5)
- **部署**: Nginx + Systemd

## 快速开始

### 环境要求

- Python 3.10+
- Linux服务器（推荐Ubuntu/Debian）
- 至少1GB内存，50GB硬盘

### 安装

```bash
# 克隆仓库
git clone https://github.com/yourusername/dola-pool.git
cd dola-pool

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务
python3 app.py
```

### 配置

1. 配置代理：在 `gen/dola_gen_clean.py` 中设置代理信息
2. 导入账号：在后台导入Dola账号Cookie
3. 配置AI推理：设置Agnes AI API Key

## API接口

### 创建视频任务

```bash
POST /api/tasks
Content-Type: application/json

{
  "prompt": "视频提示词",
  "model": "seedance_v2.0",
  "duration": 10,
  "ratio": "9:16",
  "quality": "720p"
}
```

### 查询任务状态

```bash
GET /api/tasks/{task_id}
```

### 重试失败任务

```bash
POST /api/tasks/{task_id}/retry
```

### 一键重试所有失败任务

```bash
POST /api/tasks/retry-all-failed
```

## 定价

| 模型 | 时长 | 价格 |
|------|------|------|
| SD2.0 | 10秒 | ¥1.5/条 |
| SD2.0 | 15秒 | ¥2.0/条 |
| SD2.5 | 30秒 | ¥2.5/条 |

## 项目结构

```
dola-pool/
├── app.py                    # 主程序
├── gen/
│   └── dola_gen_clean.py    # 视频生成核心（去水印版）
├── ai_reason_enhanced.py    # AI推理增强模块
├── ai_reason_module.py      # AI推理模块
├── static/
│   └── node-canvas.html     # 节点画布页面
├── scripts/
│   └── refresh-proxy.sh     # 代理刷新脚本
├── cleanup.sh               # 自动清理脚本
├── requirements.txt         # Python依赖
├── Dockerfile               # Docker配置
└── docker-compose.yml       # Docker Compose配置
```

## 注意事项

- 本项目仅供学习交流使用
- 请遵守Dola AI的使用条款
- 建议使用亚洲IP（台湾/香港/新加坡）避免风控
- 每个账号每天生成不超过2条视频

## 许可证

MIT License
