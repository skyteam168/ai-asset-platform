import { Table } from "antd";
import type { TableProps } from "antd/es/table";

/**
 * 列表表格：列宽合计超出容器时在表内横滚。
 * 避免 .aip-content 的 overflow-x:hidden 裁切表头，或把短中文标题挤成竖排。
 */
export function ListTable<RecordType extends object>(props: TableProps<RecordType>) {
  const { scroll, ...rest } = props;
  const next =
    scroll === undefined
      ? { x: "max-content" as const }
      : scroll && typeof scroll === "object" && scroll.x === undefined
        ? { x: "max-content" as const, ...scroll }
        : scroll;
  return <Table<RecordType> {...rest} scroll={next} />;
}
