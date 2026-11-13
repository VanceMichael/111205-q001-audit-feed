# 公告采集字段的可复核证据包

## 现有能力

- `service.py`：既有公告规范化接口，保持不变。
- `evidence.py`：在规范化入口旁提供可离线复核的证据链（不新建公告检索库）——
  - 抓取留痕：每次提取保存原始响应字节、响应头、抓取时刻与提取配置摘要；
  - 字段定位：结构化字段记录 JSON 指针或 HTML 元素定位，并保留该位置的原始片段；
  - 派生结论：把列表页、详情页、附件说明等输入依赖连成闭包；缺页或主体名称
    互相矛盾时标记“证据不足”，不用其他抓取批次的页面悄悄补齐旧结论；
    取值相同但来自不同批次的证据身份不同，不会混成一份证明；
  - 证据包导出：按一次复核导出独立证据包（批次钉选、清单摘要、原子落盘、
    审阅遮盖），包生成期间新到达的采集不会串入已选批次；
  - 离线复核：校验文件摘要、依赖闭包与字段定位，并重新算出展示值。
- `demo.py`：演示规范化函数，并实时演示证据链的导出、复核、遮盖、缺页与中断处理。
- `tests/`：基线回归（含原 `test_service.py`）+ 证据链自动化测试。

## 证据包格式

```
<package>/
  raw/<capture_id>.bin            原始响应字节（审阅包中为遮盖后的字节）
  raw/<capture_id>.headers.json   响应头、抓取时刻、提取配置摘要、批次号
  fields/<evidence_id>.json       字段证据：定位器、展示值、原始片段
  derived.json                    派生结论：依赖闭包、状态、展示值、批次钉选
  redaction.json                  仅审阅包：遮盖字段、定位、原因、被遮盖片段摘要
  manifest.json                   清单：全部文件摘要，最后落盘
```

导出先写 `<package>.tmp` 过渡目录，manifest.json 最后落盘后整体改名；
中断只可能留下 `*.tmp` 残留，复核一律判为未完成导出。目标路径已存在时
拒绝写入，审阅导出因此无法覆盖内部完整证据。

## 失败证明的可观察结果

`verify_package` 报告 `package_valid`、`proof_status` 与 `failures[].code`：

| 故障（fixtures/evidence.json 的 fault_cases） | 可观察结果 |
| --- | --- |
| missing-detail（缺页） | 结论 `proof_status=insufficient_evidence`，原因 `missing-input:detail-1`；包级缺文件另报 `missing-dependency` |
| tampered-raw（篡改文件） | `package_valid=false`，`failures` 含 `tampered-file` |
| stale-locator（定位失效） | `package_valid=false`，`failures` 含 `stale-locator`（页面改版后定位重放与留痕不符） |
| interrupted-export（导出中断） | 目标目录不存在或仅有 `*.tmp` 残留，复核报 `incomplete-export` |

成功的证明 = `package_valid` 为真且 `proof_status` 为 `ok`；证据不足的证明
包体可以完好但 `proof_status` 为 `insufficient_evidence`，两者不会混淆
（见 `tests/test_evidence.py` 的 ProofSeparationTests）。

## 环境和运行

使用 Python 3.11.10，仅依赖标准库，无需登录账号、安装第三方包或下载数据。`.python-version` 固定解释器版本；`pyproject.toml` 仅登记 unittest 入口，项目不需要打包安装。

在仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
```

完整演示命令：

```sh
python3 -m unittest discover -s tests -v && python3 demo.py
```

演示实时读取合成输入并运行当前接口，通常在 90 秒以内结束，不回放历史日志。状态写入临时目录，重复执行不污染仓库；本地 HTTP 示例只访问回环地址，不连接生产系统。

## 合成来源

`fixtures/pages.json`、`fixtures/notices.html` 保存合成公告；`fixtures/evidence.json` 描述跨页面证据依赖（list-1 / detail-1）、审阅遮盖范围（contact）和故障用例（missing-detail / tampered-raw / stale-locator / interrupted-export），与 `tests/test_evidence.py` 一一对应。

## 能力边界

证据存储为内存态，证据包落本地目录；HTML 定位支持标签、`.class`、`#id`、`[attr]`、后代组合子与末尾 `@属性` 取值。业务编排、持久化的流程决策与跨模块恢复不属于当前实现范围，演示输出不代表这些扩展能力已经存在。
