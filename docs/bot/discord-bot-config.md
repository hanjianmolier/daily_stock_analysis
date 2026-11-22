# Discord机器人配置

## Discord机器人
Discord机器人接收消息需要使用Discord Developer Portal创建机器人应用
https://discord.com/developers/applications

Discord机器人支持两种消息发送方式：
1. **Webhook模式**：配置简单，权限低，适合只需要发送消息的场景
2. **Bot API模式**：权限高，支持接收命令，需要配置Bot Token和频道ID

## 创建Discord机器人

### 1. 登录Discord Developer Portal
访问 https://discord.com/developers/applications 并使用你的Discord账号登录

### 2. 创建应用
点击"New Application"按钮，输入应用名称（例如：A股智能分析机器人），然后点击"Create"

### 3. 配置机器人
在左侧导航栏中点击"Bot"，然后点击"Add Bot"按钮，确认添加

### 4. 获取Bot Token
在Bot页面，点击"Reset Token"按钮，然后复制生成的Token（这是你的`DISCORD_BOT_TOKEN`）

### 5. 配置权限
在Bot页面的"Privileged Gateway Intents"部分，开启以下选项：
- Presence Intent
- Server Members Intent
- Message Content Intent

### 6. 添加到服务器
1. 在左侧导航栏中点击"OAuth2" > "URL Generator"
2. 在"Scopes"中选择：
   - `bot`
   - `applications.commands`
3. 在"Bot Permissions"中选择：
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
   - Use Slash Commands
4. 复制生成的URL，在浏览器中打开，选择要添加机器人的服务器

### 7. 获取频道ID
1. 在Discord客户端中，开启开发者模式：设置 > 高级 > 开发者模式
2. 右键点击你想要机器人发送消息的频道，选择"Copy ID"（这是你的`DISCORD_MAIN_CHANNEL_ID`）

## 配置环境变量

将以下配置添加到你的`.env`文件中：

```env
# Discord 机器人配置
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_MAIN_CHANNEL_ID=your-channel-id
DISCORD_WEBHOOK_URL=your-webhook-url (可选)
DISCORD_BOT_STATUS=A股智能分析 | /help
```

## Webhook模式配置（可选）

如果你只想使用Webhook模式发送消息，不需要Bot Token，可以按照以下步骤配置：

1. 右键点击频道，选择"编辑频道"
2. 点击"集成" > "Webhooks" > "新建Webhook"
3. 配置Webhook名称和头像
4. 复制Webhook URL（这是你的`DISCORD_WEBHOOK_URL`）

## Slash 命令（Interactions Endpoint）

Slash 命令通过 Discord 的 Interactions Endpoint 回调到本服务的 `POST /bot/discord`，因此需要服务（`python main.py --serve` 或 Docker）能被公网 HTTPS 访问。未配置 `DISCORD_PUBLIC_KEY` 时该端点返回 404。

### 1. 配置公钥

在 Developer Portal 的 **General Information** 页面复制 **Public Key**，写入 `.env`（或 WebUI 系统设置）：

```env
DISCORD_PUBLIC_KEY=your-application-public-key
```

所有请求都会用该公钥做 Ed25519 签名验证，签名无效的请求返回 401，不会执行任何命令。

### 2. 设置回调地址

在 **General Information** 页面将 **Interactions Endpoint URL** 设置为：

```
https://<你的域名>/bot/discord
```

保存时 Discord 会发送校验请求（PING），服务已启动且公钥正确才能保存成功。

### 3. 注册 Slash 命令

命令名需与机器人命令一致。以下示例使用 Bot Token 注册全局命令（`APPLICATION_ID` 在 General Information 页面获取）：

```bash
curl -X PUT "https://discord.com/api/v10/applications/$APPLICATION_ID/commands" \
  -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '[
    {"name": "analyze", "description": "分析指定股票", "options": [
      {"name": "stock_code", "description": "股票代码，如 600519 / HK00700 / AAPL", "type": 3, "required": true},
      {"name": "full_report", "description": "是否生成完整报告", "type": 5}
    ]},
    {"name": "market", "description": "大盘复盘"},
    {"name": "chat", "description": "与 Agent 对话（需 AGENT_MODE=true）", "options": [
      {"name": "question", "description": "问题", "type": 3, "required": true}
    ]},
    {"name": "status", "description": "系统状态"},
    {"name": "help", "description": "查看帮助"}
  ]'
```

选项按顺序拼接为文本命令，例如 `/analyze stock_code:600519 full_report:True` 等价于 `/analyze 600519 full`；值为 False 的布尔选项会被忽略。

### 4. 响应方式

- 收到命令后先立即返回“正在思考”（延迟响应），命令执行完成后自动更新为结果，最长 2000 字。
- `/analyze`、`/market` 等异步任务的完整报告仍通过已配置的通知渠道（如 `DISCORD_WEBHOOK_URL`）推送。
- 回复不会触发 @everyone 或角色提醒。

## 测试机器人

1. 确保机器人已成功添加到你的服务器，且 Interactions Endpoint URL 已保存成功
2. 在频道中输入`/help`，机器人会返回帮助信息
3. 输入`/analyze 600519`测试股票分析功能
4. 输入`/market`测试大盘复盘功能

## 注意事项

1. 确保你的机器人有足够的权限在频道中发送消息和使用Slash命令
2. 定期更新你的Bot Token，确保安全性
3. 不要将你的Bot Token分享给任何人
4. 如果机器人没有响应，检查：
   - Bot Token是否正确
   - 频道ID是否正确
   - 机器人是否在线
   - 机器人是否有消息发送权限

## 故障排除

- **机器人不响应命令**：检查Bot Token和频道ID是否正确，确保机器人已添加到服务器
- **Slash命令不显示**：确认已按上文注册命令；全局命令可能需要等待同步，或重新添加机器人
- **Interactions Endpoint URL 保存失败**：确认服务可通过公网 HTTPS 访问 `/bot/discord`，且 `DISCORD_PUBLIC_KEY` 与当前应用一致
- **命令提示“交互失败”**：查看服务日志中的 `[Discord]` 记录，常见原因是签名验证失败或回填结果时网络不通
- **消息发送失败**：检查频道权限，确保机器人有发送消息的权限

## 相关链接

- [Discord Developer Portal](https://discord.com/developers/applications)
- [Discord Bot Documentation](https://discordpy.readthedocs.io/en/stable/)
- [Discord Slash Commands](https://discord.com/developers/docs/interactions/application-commands)
