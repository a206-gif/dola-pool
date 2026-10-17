把浏览器扩展放在这里，程序就能用「带扩展的浏览器」打开 Dola。

现在自带两套（来自你上传的 Extensions.zip，原样保留）：

  dola30/        哆啦助手 · 30秒无水印助手 v1.6.12
                 · 视频生成页的「30 秒」开关（Seedance 2.5 的 30s）
                 · 720P / 1080P 分辨率注入
                 · 无水印原图 / 原视频提取（右下角浮动面板）
  cookie-editor/ Cookie-Editor v1.13.0
                 · 查看 / 导出当前站点的 Cookie（复制 CK 进账号池用）

目录要求：
  extensions/<任意名字>/manifest.json
  （可以再套一层，例如 extensions/Extensions/dola30/ 也能识别）

程序里的用法：
  账号池 → 【🧩 浏览器扩展】卡片 → 【🧩 带扩展浏览器打开】
  它等价于这条命令：
    msedge.exe --user-data-dir=<数据目录>\extension-browser
               --no-first-run --no-default-browser-check
               --disable-extensions-except=<ext1>,<ext2>
               --load-extension=<ext1>,<ext2> https://www.dola.com/chat/

关于 Chrome 137+：
  谷歌从 Chrome 137 起删掉了 --load-extension（只影响 Chrome 官方版，
  Edge / Chromium / Chrome for Testing 仍然支持）。所以程序优先用 Edge；
  只有 Chrome 时会提示你先在扩展管理页手动加载一次（一次性，之后这个
  独立 profile 会记住）。卡片上的【⚙ 手动加载】按钮就是干这个的。

注意：
· 扩展是给真正的 Edge / Chrome 用的，WebView2 内核不支持加载扩展
  （微软要求建环境时开 AreBrowserExtensionsEnabled，pywebview 没暴露这个开关）。
· 登录一次之后会记住（profile 存在数据目录里），下次打开还是登录态。
· 扩展的行为由扩展自己负责，本程序只负责把它装进浏览器启动参数里。
· 30 秒能不能真的出片最终由 Dola 服务端决定，被拒时网页会提示时长范围。
