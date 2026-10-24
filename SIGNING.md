# 代码签名

当前 `CountdownReminder.exe` **没有代码签名**，所以第一次运行时 Windows SmartScreen 可能提示“未知发布者”。  
这不是 bug，是证书和信誉问题；**自签名证书不会消除这个提示**，反而仍然显示未知发布者。

## 可选路径

| 方案 | 费用 | 适合 | 说明 |
|---|---|---|---|
| **SignPath Foundation** | 免费（开源项目） | 最推荐 | 面向开源项目的免费代码签名，通过 GitHub Actions 集成 |
| **Certum Open Source Code Signing** | 低价（需申请） | 个人开源项目 | 波兰 Certum 的开源证书，性价比高 |
| OV 代码签名证书 | 约每年几百元 | 长期分发 | 需要个人/企业实名，SmartScreen 信誉逐步积累 |
| EV 代码签名证书 | 更贵 | 商业分发 | 硬件令牌，SmartScreen 信誉最好，学生项目没必要 |
| Azure Trusted Signing | 订阅制 | 有实体的组织 | 个人/学生通常不满足资格 |

> 不要用 `makecert` / 自签名证书去签正式发布包。系统仍会认为发布者不可信。

## 推荐：SignPath Foundation

1. 打开 https://signpath.org/ ，用 GitHub 账号申请开源项目；
2. 填项目地址：`https://github.com/387wsx/TimeNote`；
3. License 选 MIT；
4. 构建方式说明：PyInstaller 单文件，产物 `dist/CountdownReminder.exe`；
5. 通过后，在 SignPath 里配置 GitHub Actions，把签名步骤接到 release 流程。

申请时可以直接用这段：

```text
Project: TimeNote
Repository: https://github.com/387wsx/TimeNote
License: MIT
Platform: Windows 10/11 x64
Build: PyInstaller 6.x, one-file windowed executable
Artifact: dist/CountdownReminder.exe
Description: A lightweight countdown reminder desktop app for Windows.
```

## 拿到证书后怎么签

如果拿到的是 `.pfx` 文件：

```bat
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
  /f your-cert.pfx /p your-password ^
  dist\CountdownReminder.exe

signtool verify /pa /v dist\CountdownReminder.exe
```

时间戳很重要：证书过期后，已签名的旧版本仍然有效。

## GitHub Actions 签名

把证书转成 base64 放进仓库 Secrets：

- `WINDOWS_CERT_PFX_BASE64`
- `WINDOWS_CERT_PASSWORD`

工作流里解码成临时 `.pfx`，构建后执行 `signtool sign`，再上传 Release。  
证书和密码不要提交到仓库。

## 签名之后的预期

- 首次下载仍可能被 SmartScreen 拦一次，但发布者会显示你的名字/组织；
- 随着下载量增加，SmartScreen 信誉会逐步建立；
- EV 证书可以更快建立信誉，但成本高；
- 签名不能代替杀毒软件白名单，只能证明“这个 exe 是谁发的、发布后没被改过”。

## 现阶段建议

先申请 **SignPath Foundation**；批下来之前，README 里保留 SmartScreen 提示即可。  
不想折腾证书也完全没问题：这是个人开源小工具，未签名并不影响本地使用。
