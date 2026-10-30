# 会议室预约服务缺陷修复

你接手了学校的一段会议室预约代码。用户反馈：前一场会议结束时，下一场无法立即开始；取消预约后，原时间段仍然无法重新预约。请阅读现有工程、复现问题、修复逻辑，并补充至少一个回归测试。

这是 VibeEval 一期的完整样题，用于验证浏览文件、修改代码、运行测试与提交答卷的流程。题目无需网页、数据库、网络或外部服务。

## 业务规则

- 同一会议室的有效预约不能重叠；前一场在 10:00 结束，后一场可以在 10:00 开始。此规则与创建预约的先后顺序无关。
- 不同会议室互不影响，取消的预约不再占用会议室。
- 开始时间必须早于结束时间；空会议室编号不合法。会议室编号去除首尾空白后区分大小写，不需要查询预设会议室清单。
- 时间必须为不带时区的 Python `datetime`，统一按校园本地时间处理。不读取系统当前时间，不限制预约必须发生在未来。
- 取消不存在的预约抛出 `ValueError`；重复取消同一预约应成功，返回同一预约编号的已取消记录。
- 每个 `BookingService` 独立在内存中管理预约，程序退出后数据不保留。不要求实现并发、多用户权限或持久化。

## 已有接口

`src/booking.py` 提供：

| 接口 | 行为 |
| --- | --- |
| `BookingService()` | 创建一个独立的预约服务 |
| `reserve(room_id, start, end) -> Booking` | 创建预约；冲突或非法输入抛出 `ValueError` |
| `cancel(booking_id) -> Booking` | 取消预约并返回已取消记录 |

`Booking` 的字段是 `booking_id`、`room_id`、`start`、`end`、`cancelled`。编号为非空字符串，在同一服务内不重复。返回记录是不可变快照，取消后以 `cancel` 返回的新记录为准。

## 运行与复现

需要 Python 3.12，无需安装第三方库。在本题目录执行：

```powershell
python src/main.py
python -m unittest discover -s tests -p "test_*.py" -v
```

`main.py` 是固定场景演示，不是交互式预约系统。原始版本应显示一个场景通过、两个场景未通过；脚本正常退出，是否修复应以测试为准。

公开测试共 8 项，原始版本有且仅有以下两项失败，其他 6 项通过，没有导入或运行错误。测试命令返回非零退出码是题目的预期初始状态：

- `test_adjacent_reservations_allowed`：允许首尾相接的预约。
- `test_cancel_releases_room`：取消后应允许重新预约原时段。

修复后公开测试应全部通过。如果你的 Python 命令不可用，可使用 `python3`，或指定 Python 3.12 解释器的完整路径。

## 作答范围

- 修改 `src/booking.py`；保留上述类、方法签名、返回字段与既有业务规则。
- 在 `tests/test_candidate.py` 添加至少一个回归测试，不修改 `tests/test_public.py`、演示脚本或依赖声明。
- 新测试应验证真实业务行为，可以考虑另一方向的相邻预约、取消与其他有效预约组合等尚未充分覆盖的场景。测试使用 `unittest`，不得只调用函数而不检查结果，也不应依赖内部存储方式。
- 用同一个测试命令运行公开测试及新增测试，再提交修改后的工程。

新建的 `tests/test_candidate.py` 需要独立设置模块路径，不能依赖公开测试先被导入。可用下面的文件开头，再编写自己的 `unittest.TestCase` 和 `test_` 方法：

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from booking import BookingService
```

## 文件与发布边界

```text
python-debug-01/
├── README.md
├── requirements.txt
├── src/
│   ├── booking.py
│   └── main.py
└── tests/
    └── test_public.py
```

此目录内是考生工程；平台发布时只挂载此目录。参考修复和维护者验证工具不属于考生材料，也不应随答题工程或提交快照一起打包。
