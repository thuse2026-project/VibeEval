# 阶段一 A：仓库探索（20 分钟）

你接手了一个使用 TinyDB 保存本地数据的 Python 项目。请理解这个库的用途、核心模块、主要 API 与数据操作过程，为之后独立解释代码做好准备。

重点阅读 repository/tinydb 中的 database.py、table.py、queries.py、storages.py；__init__.py 可帮助确认公共入口。其他文件可按需查看，不要求掌握所有查询操作符、扩展、中间件、类型检查插件、并发或事务。

允许：手动浏览和搜索源码；用 Cursor Ask 询问当前仓库；自行编辑 NOTES.md。
禁止：修改源码、执行代码或测试、使用终端或联网、访问其他阶段或参考材料、让 AI 代写笔记。

以下只展示典型 API 用法，不要求执行：

```python
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage
db = TinyDB(storage=MemoryStorage)
users = db.table('users')
users.insert({'name': 'Ada', 'age': 20})
users.search(Query().age >= 18)
db.close()
```

## 开始问答时先发送

> 我正在进行 20 分钟仓库探索。请只读取和搜索当前工作区的 repository/tinydb，按我的问题帮助理解源码，并引用文件与函数。不要修改任何文件、运行命令或代码、联网、使用外部 MCP、访问父目录或其他工作区，也不要创建子代理。不要替我写笔记或预测后续考试题。遇到不确定之处请明确说明。

先记录模型和开始时间，再计时。到时停止对话，将自己的 NOTES.md 复制到下一工作区。不要携带聊天记录。
