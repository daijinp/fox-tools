# convert_ck_sql

把 DataGrip 导出的 ClickHouse `INSERT` 语句里这种：

```sql
{key=value, other=123}
```

转换成 ClickHouse 能执行的：

```sql
map('key', 'value', 'other', 123)
```

## 用法

1. 把 DataGrip 导出的原始 SQL 放到同目录的 `input.sql`
2. 按需要修改同目录的 `config.json`
3. 运行：

```powershell
python .\convert_datagrip_ck_insert.py
```

运行完成后会生成 `output.sql`。

## 配置文件

默认配置文件是 [config.json](E:\MyCode\pythonProject\tools\convert_ck_sql\config.json)：

```json
{
  "input_file": "input.sql",
  "output_file": "output.sql",
  "empty_map_default_type": "Float64",
  "print_inferred_types": true,
  "map_type_overrides": {
    "inverter_status": "String",
    "battery_status": "String"
  }
}
```

字段说明：

- `input_file`: 原始 SQL 文件名或绝对路径
- `output_file`: 转换后 SQL 文件名或绝对路径
- `empty_map_default_type`: 空 map 默认类型，可选 `String`、`Float64`、`Int64`、`UInt8`
- `print_inferred_types`: 运行后是否打印推断出来的 map 类型
- `map_type_overrides`: 手动指定某些列的 map 类型

## 说明

- 非空 map 会自动按每列值风格推断成 `String` / `Float64` / `Int64`
- 空 map 会输出成 `CAST(map(), 'Map(String, TYPE)')`
- 只处理 `INSERT ... VALUES ...`，其他 SQL 原样保留
