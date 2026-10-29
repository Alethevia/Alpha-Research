# 运行与真实性审查 — 2026-10-29

## 修复安装元数据，撤下未经复现的收益结论

安装依赖统一由requirements.txt提供；重型可选依赖保留requirements-all.txt。移除指向不存在模块的alpha-validate命令。README撤下无原始数据、运行配置和复现日志支撑的业绩表；旧研究文档标明历史讨论，不能充当第三方认证。

## 本次执行证据

281项现有测试通过；可编辑安装及alpha-research --help通过。

```text
python -m pip install -e ".[dev]"
python -m pytest tests -q -o addopts=
alpha-research --help
```

Python检查使用Linux/Python 3.12。测试结果是本次本地执行记录，不代表GitHub Actions已通过，也不是独立认证。

## 仍未验证的范围

本次没有重跑全部真实数据策略、交易服务或论文实证；历史研究文档中的结果不因此获得验证。

删除仅针对已确认失效、重复说明或可再生成产物。源码、必要资产和不同版本保留；更改可从Git提交历史恢复。
