"""
{
    "segmented_time_mode1__time1_enable": "enable",
    "segmented_time_mode1__time1_work_mode": "SelfUse",
    "segmented_time_mode1__time1_max_soc": "100",
    "segmented_time_mode1__time1_fd_pwr": "5000",
    "segmented_time_mode1__time1_fd_soc": "20",
    "segmented_time_mode1__time1_min_soc_on_grid": "10",
    "segmented_time_mode1__time1_start_hour": "19",
    "segmented_time_mode1__time1_start_minute": "1",
    "segmented_time_mode1__time1_end_hour": "19",
    "segmented_time_mode1__time1_end_minute": "29",
    # --------------------------------------------------------------
    "segmented_time_mode1__time3_start_hour": "0",
    "segmented_time_mode1__time8_fd_pwr": "0",
    "segmented_time_mode1__time7_end_minute": "0",
    "segmented_time_mode1__time2_min_soc_on_grid": "10",
    "segmented_time_mode1__time5_start_minute": "0",
    "segmented_time_mode1__time6_fd_pwr": "0",
    "segmented_time_mode1__time4_fd_pwr": "0",
    "segmented_time_mode1__time5_end_hour": "0",
    "segmented_time_mode1__time5_fd_pwr": "0",
    "segmented_time_mode1__time7_fd_pwr": "0",
    "segmented_time_mode1__time2_end_hour": "23",
    "segmented_time_mode1__time3_start_minute": "0",
    "segmented_time_mode1__time2_enable": "disable",
    "segmented_time_mode1__time3_work_mode": "SelfUse",
    "segmented_time_mode1__time4_max_soc": "100",
    "segmented_time_mode1__time5_enable": "disable",
    "segmented_time_mode1__time6_enable": "disable",
    "segmented_time_mode1__time6_work_mode": "SelfUse",
    "segmented_time_mode1__time7_start_minute": "0",
    "segmented_time_mode1__time6_min_soc_on_grid": "10",
    "segmented_time_mode1__time7_enable": "disable",
    "segmented_time_mode1__time2_max_soc": "100",
    "segmented_time_mode1__time3_enable": "disable",
    "segmented_time_mode1__time4_enable": "disable",
    "segmented_time_mode1__time8_enable": "disable",
    "segmented_time_mode1__time7_max_soc": "100",
    "segmented_time_mode1__time5_end_minute": "0",
    "segmented_time_mode1__time5_max_soc": "100",
    "segmented_time_mode1__time4_work_mode": "SelfUse",    
    "segmented_time_mode1__80": "0",
    "segmented_time_mode1__time2_fd_pwr": "5000",  
    "segmented_time_mode1__time2_start_hour": "19",
    "segmented_time_mode1__time3_fd_soc": "10",
    "segmented_time_mode1__time4_end_minute": "0",
    "segmented_time_mode1__time3_end_hour": "0",
    "segmented_time_mode1__time8_end_hour": "0",
    "segmented_time_mode1__time7_work_mode": "SelfUse",
    "segmented_time_mode1__time7_fd_soc": "10",
    "segmented_time_mode1__time6_fd_soc": "10",
    "segmented_time_mode1__time6_start_minute": "0",
    "segmented_time_mode1__time8_fd_soc": "10",
    "segmented_time_mode1__time8_work_mode": "SelfUse",
    "segmented_time_mode1__time4_fd_soc": "10",
    "segmented_time_mode1__time5_fd_soc": "10",
    "segmented_time_mode1__time4_start_minute": "0",
    "segmented_time_mode1__time4_min_soc_on_grid": "10",
    "segmented_time_mode1__time2_start_minute": "29",
    "segmented_time_mode1__time7_min_soc_on_grid": "10",
    "segmented_time_mode1__time4_end_hour": "0",
    "segmented_time_mode1__time7_end_hour": "0",
    "segmented_time_mode1__time3_max_soc": "100",
    "segmented_time_mode1__time6_end_minute": "0",
    "segmented_time_mode1__time3_end_minute": "0",
    "segmented_time_mode1__time4_start_hour": "0",
    "segmented_time_mode1__time3_min_soc_on_grid": "10",
    "segmented_time_mode1__time8_start_minute": "0",
    "segmented_time_mode1__time8_max_soc": "100",
    "segmented_time_mode1__time8_start_hour": "0",
    "segmented_time_mode1__time2_end_minute": "59",
    "segmented_time_mode1__time5_start_hour": "0",
    "segmented_time_mode1__time6_end_hour": "0",
    "segmented_time_mode1__time6_max_soc": "100",
    "segmented_time_mode1__time8_end_minute": "0",
    "segmented_time_mode1__time3_fd_pwr": "0",
    "segmented_time_mode1__time5_min_soc_on_grid": "10",
    "segmented_time_mode1__time5_work_mode": "SelfUse",
    "segmented_time_mode1__time7_start_hour": "0",
    "segmented_time_mode1__time8_min_soc_on_grid": "10",
    "segmented_time_mode1__time2_fd_soc": "20",
    "segmented_time_mode1__time6_start_hour": "0",
    "segmented_time_mode1__time2_work_mode": "SelfUse"
}


keys = [
    "segmented_time_mode1__time1_enable",
    "segmented_time_mode1__time1_work_mode",
    "segmented_time_mode1__time1_max_soc",
    "segmented_time_mode1__time1_fd_pwr",
    "segmented_time_mode1__time1_fd_soc",
    "segmented_time_mode1__time1_min_soc_on_grid",
    "segmented_time_mode1__time1_start_hour",
    "segmented_time_mode1__time1_start_minute",
    "segmented_time_mode1__time1_end_hour",
    "segmented_time_mode1__time1_end_minute"
]
"""
def get_keys():
    after_keys_prefix = 'segmented_time_mode1__time'
    before_keys = [
            "_enable",
            "_work_mode",
            "_max_soc",
            "_fd_pwr",
            "_fd_soc",
            "_min_soc_on_grid",
            "_start_hour",
            "_start_minute",
            "_end_hour",
            "_end_minute"
    ]

    keys = []
    for i in range(1, 9):
        key = f"{after_keys_prefix}{i}"
        for before_key in before_keys:
            keys.append(key + before_key)
    return keys


def sort_dict_by_keys(data):
    """
    判断传入的字典是否包含get_keys返回的所有key，如果包含则按顺序排序
    
    参数:
        data: 待处理的数据
        
    返回:
        - 如果不是字典，抛出 TypeError
        - 如果不包含所有key，原样返回
        - 如果包含所有key，返回按get_keys顺序排序后的字典
    """
    # 判断是否为字典
    if not isinstance(data, dict):
        raise TypeError(f"期望传入字典类型，但收到的是 {type(data).__name__}")
    
    # 获取标准key列表
    standard_keys = get_keys()
    
    # 判断字典是否包含所有标准key
    dict_keys = set(data.keys())
    standard_keys_set = set(standard_keys)
    
    # 如果不包含所有标准key，原样返回
    if not standard_keys_set.issubset(dict_keys):
        return data
    
    # 包含所有key，进行排序
    # 先按照标准顺序添加key，然后添加剩余的key
    sorted_dict = {}
    
    # 按照get_keys的顺序添加key
    for key in standard_keys:
        if key in data:
            sorted_dict[key] = data[key]
    
    # 添加不在标准keys中的其他key（保持原有顺序）
    for key in data:
        if key not in standard_keys_set:
            sorted_dict[key] = data[key]
    f"""
    _enable 启用开关
    _work_mode 工作模式
    _max_soc 最大SOC
    _fd_pwr 放电功率
    _fd_soc 放电SOC
    _min_soc_on_grid 接电网的最低SOC
    _start_hour 开始小时
    _start_minute 开始分钟
    _end_hour 结束小时
    _end_minute 结束分钟
    """
    cn_res = []
    after_keys_prefix = 'segmented_time_mode1__time'

    # 对 结果进行 中文解析
    # 如果segmented_time_mode1__time{1-8}_enable = disable 则移除
    for i in range(1, 9):
        key = f"segmented_time_mode1__time{i}_enable"
        if sorted_dict[key] == "disable":
            pass
        else:
            enable = sorted_dict[f"{after_keys_prefix}{i}_enable"]
            start_hour = sorted_dict[f"{after_keys_prefix}{i}_start_hour"]
            start_minute = sorted_dict[f"{after_keys_prefix}{i}_start_minute"]
            end_hour = sorted_dict[f"{after_keys_prefix}{i}_end_hour"]
            end_minute = sorted_dict[f"{after_keys_prefix}{i}_end_minute"]
            group = {    
                f"开启时段-{i}(enable)": enable,
                f"开区时间段-{i}": f"{start_hour}时,{start_minute}分-{end_hour}时,{end_minute}分",
                f"工作模式-{i}(work_mode)": sorted_dict[f"{after_keys_prefix}{i}_work_mode"],
                f"最大SOC-{i}(max_soc)": sorted_dict[f"{after_keys_prefix}{i}_max_soc"],
                f"放电功率-{i}(fd_pwr)": sorted_dict[f"{after_keys_prefix}{i}_fd_pwr"],
                f"放电SOC-{i}(fd_soc)": sorted_dict[f"{after_keys_prefix}{i}_fd_soc"],
                f"接电网的最低SOC-{i}(min_soc_on_grid)": sorted_dict[f"{after_keys_prefix}{i}_min_soc_on_grid"]
            }
            cn_res.append(group)
    segmented_time_mode1__80 = sorted_dict.get('segmented_time_mode1__80')
    cn_res.append({'segmented_time_mode1__80': segmented_time_mode1__80})
    return cn_res


# 测试代码
if __name__ == "__main__":
    import json
    
    print("=" * 60)
    print("测试1: 包含所有key的字典（顺序混乱）")
    print("=" * 60)
    test_dict1 = {
        "segmented_time_mode1__time1_end_minute": "10",
        "segmented_time_mode1__time1_enable": "enable",
        "segmented_time_mode1__time1_start_hour": "19",
        "segmented_time_mode1__time1_work_mode": "SelfUse",
        "segmented_time_mode1__time1_max_soc": "100",
        "segmented_time_mode1__time1_fd_pwr": "5000",
        "segmented_time_mode1__time1_fd_soc": "20",
        "segmented_time_mode1__time1_min_soc_on_grid": "10",
        "segmented_time_mode1__time1_start_minute": "1",
        "segmented_time_mode1__time1_end_hour": "19",
        # ... 其他time2-time8的key（这里省略，实际使用需要包含所有80个key）
    }
    
    # 为了测试，补全所有必需的key
    all_keys = get_keys()
    for key in all_keys:
        if key not in test_dict1:
            test_dict1[key] = "0"
    
    # 添加一些额外的key
    test_dict1["extra_key_1"] = "extra_value_1"
    test_dict1["extra_key_2"] = "extra_value_2"
    
    print("原始字典前5个key:", list(test_dict1.keys())[:5])
    result1 = sort_dict_by_keys(test_dict1)
    print("排序后字典前5个key:", list(result1.keys())[:5])
    print("排序后字典总key数:", len(result1))
    print()
    
    print("=" * 60)
    print("测试2: 不包含所有key的字典（原样返回）")
    print("=" * 60)
    test_dict2 = {
        "segmented_time_mode1__time1_enable": "enable",
        "segmented_time_mode1__time1_work_mode": "SelfUse",
        "some_other_key": "value"
    }
    print("原始字典keys:", list(test_dict2.keys()))
    result2 = sort_dict_by_keys(test_dict2)
    print("返回字典keys:", list(result2.keys()))
    print("是否为同一对象:", result2 is test_dict2)
    print()
    
    print("=" * 60)
    print("测试3: 传入非字典类型（应该报错）")
    print("=" * 60)
    try:
        result3 = sort_dict_by_keys("not a dict")
        print("未报错（这不应该发生）")
    except TypeError as e:
        print(f"✓ 成功捕获错误: {e}")
    print()
    
    print("=" * 60)
    print("测试4: 传入列表类型（应该报错）")
    print("=" * 60)
    try:
        result4 = sort_dict_by_keys([1, 2, 3])
        print("未报错（这不应该发生）")
    except TypeError as e:
        print(f"✓ 成功捕获错误: {e}")