# Geo System Diagrams

本目录保存 Geo 协作平台的系统图源文件。

- `01-system-overview.mmd`: 系统总览图
- `02-data-flow.mmd`: 主数据流图
- `03-pipeline-flow.mmd`: Pipeline/智能体运行图
- `04-core-tables-er.mmd`: 核心数据表关系图
- `05-module-dependencies.mmd`: 模块依赖图

如果已安装 Mermaid CLI，可以在仓库根目录渲染：

```powershell
pnpm dlx @mermaid-js/mermaid-cli -i pic/01-system-overview.mmd -o pic/01-system-overview.svg
pnpm dlx @mermaid-js/mermaid-cli -i pic/02-data-flow.mmd -o pic/02-data-flow.svg
pnpm dlx @mermaid-js/mermaid-cli -i pic/03-pipeline-flow.mmd -o pic/03-pipeline-flow.svg
pnpm dlx @mermaid-js/mermaid-cli -i pic/04-core-tables-er.mmd -o pic/04-core-tables-er.svg
pnpm dlx @mermaid-js/mermaid-cli -i pic/05-module-dependencies.mmd -o pic/05-module-dependencies.svg
```
