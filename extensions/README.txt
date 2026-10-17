把浏览器扩展放在这里，程序就能用「带扩展的浏览器」打开 Dola。

目录要求：
  extensions/<任意名字>/manifest.json
  （可以有嵌套，例如 extensions/Extensions.zip 解压出来的 extensions/dola30/）

程序里的用法：
  账号池 → 【🧩 带扩展浏览器】→ 选择浏览器 → 打开 Dola
  它等价于这条命令：
    msedge.exe --user-data-dir=<数据目录>\extension-browser
               --disable-extensions-except=D:\...\extensions\dola30
               --load-extension=D:\...\extensions\dola30 https://www.dola.com/chat/

注意：
· 扩展是给真正的 Edge / Chrome 用的，WebView2 内核不支持加载扩展
  （微软要求建环境时开 AreBrowserExtensionsEnabled，pywebview 没暴露这个开关）。
· 登录一次之后会记住（profile 存在数据目录里），下次打开还是登录态。
· 扩展的行为由扩展自己负责，本程序只负责把它装进浏览器启动参数里。
