"""质量越界区间归并测试：观测顺序、重复提交、规则版本、工单回写和复测回查。

场景来自真实处置流程：
- 温度测点上限 8.5，早班 8.7、9.2 与午班 8.2 依次交给同一巡检员；
- 处置人刚点完修复，同一规则又进来两笔越界，需并入原区间而不是另起调查；
- 同一份早班报文再投晚班，不能算成晚班新发现；
- 规则换过阈值后，旧观测保留各自的规则版本。
"""
import app.store as store_mod
from app.services import breach_ledger as ledger
from app.services.issue_track import IssueTrackService
from app.services.quality_rule import QualityRuleService


def _fresh():
    store_mod.store.reset()
    return QualityRuleService(), IssueTrackService()


def _temperature_rule(q, asset="温度测点A", point="TP-01", inspector="巡检员甲", high=8.5):
    return q.define_rule(
        asset, "温度", "", high, "阻断",
        point_code=point, inspector=inspector, rule_code="QUAL-TEMP1",
    )


class TestObservationOrderAndDedup:

    def test_breaches_merge_into_one_interval_in_observation_order(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        first = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                observed_at="2026-11-21 06:10", shift="早班", message_id="M-early")
        second = q.execute_check(rule["id"], 9.2, point_code="TP-01",
                                 observed_at="2026-11-21 06:40", shift="早班", message_id="M-early")
        third = q.execute_check(rule["id"], 8.2, point_code="TP-01",
                                observed_at="2026-11-21 12:10", shift="午班", message_id="M-noon")

        assert first["breached"] and second["breached"] and not third["breached"]
        assert first["interval_id"] == second["interval_id"] == third["interval_id"]
        interval = ledger.get_interval(first["interval_id"])
        assert interval["越界次数"] == 2
        assert [item["观测值"] for item in interval["观测明细"]] == [8.7, 9.2]
        assert [item["班次"] for item in interval["观测明细"]] == ["早班", "早班"]
        assert interval["首次越界值"] == 8.7
        assert interval["最新越界值"] == 9.2
        assert interval["峰值"] == 9.2
        assert interval["最新复测值"] == 8.2
        assert interval["状态"] == "待复测"

    def test_replayed_batch_counts_once(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        first = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                observed_at="2026-11-21 06:10", shift="早班", message_id="M-early")
        # 同一份早班报文再送到晚班：报文编号、测点、值都相同。
        replay = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                 observed_at="2026-11-21 06:10", shift="晚班", message_id="M-early")
        assert replay["duplicate"] is True
        assert replay["counted"] is False
        assert replay["interval_id"] == first["interval_id"]
        interval = ledger.get_interval(first["interval_id"])
        assert interval["越界次数"] == 1
        assert len(ledger.list_observations(rule_id=rule["id"])) == 1

    def test_repeated_submission_without_time_counts_once(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        q.execute_check(rule["id"], 9.2, point_code="TP-01")
        again = q.execute_check(rule["id"], 9.2, point_code="TP-01")
        assert again["duplicate"] is True
        assert ledger.get_interval(again["interval_id"])["越界次数"] == 1

    def test_same_value_at_different_time_is_two_readings(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        q.execute_check(rule["id"], 9.2, point_code="TP-01", observed_at="2026-11-21 06:40")
        later = q.execute_check(rule["id"], 9.2, point_code="TP-01", observed_at="2026-11-21 07:40")
        assert later["duplicate"] is False
        assert ledger.get_interval(later["interval_id"])["越界次数"] == 2

    def test_two_points_under_same_rule_share_interval_but_keep_both_points(self):
        q, _ = _fresh()
        rule = _temperature_rule(q, point="TP-01")
        a = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                            observed_at="2026-11-21 06:10", shift="早班")
        b = q.execute_check(rule["id"], 8.9, point_code="TP-02",
                            observed_at="2026-11-21 06:20", shift="早班")
        assert a["interval_id"] == b["interval_id"]
        interval = ledger.get_interval(a["interval_id"])
        assert interval["越界测点数"] == 2
        assert sorted(ledger._point_codes(interval)) == ["TP-01", "TP-02"]


class TestThresholdVersions:

    def test_threshold_change_keeps_old_observations_on_old_version(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        old = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                              observed_at="2026-11-21 06:10", shift="早班")
        change = q.revise_threshold(rule["id"], threshold_high=9.0, reason="夏季高温放宽")
        assert change["changed"] is True
        assert change["new_version"] == "2"

        new = q.execute_check(rule["id"], 8.7, point_code="TP-02",
                              observed_at="2026-11-21 13:10", shift="午班")
        assert new["breached"] is False
        assert new["rule_version"] == "2"
        # 新版本下的合规观测不会混入旧版本的越界区间。
        assert "interval_id" not in new
        assert ledger.get_interval(old["interval_id"])["越界次数"] == 1

        versions = q.get_rule_versions(rule["id"])
        assert versions["当前版本"] == "2"
        assert "1" in versions["区间版本"] and "2" not in versions["区间版本"]

        old_obs = ledger.list_observations(rule_id=rule["id"], version="1")
        new_obs = ledger.list_observations(rule_id=rule["id"], version="2")
        assert len(old_obs) == 1 and len(new_obs) == 1
        assert old_obs[0]["观测值"] == 8.7

    def test_new_version_breach_opens_its_own_interval(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        q.execute_check(rule["id"], 9.2, point_code="TP-01",
                        observed_at="2026-11-21 06:40", shift="早班")
        q.revise_threshold(rule["id"], threshold_high=8.0, reason="收紧上限")
        tightened = q.execute_check(rule["id"], 8.2, point_code="TP-01",
                                    observed_at="2026-11-21 12:10", shift="午班")
        assert tightened["breached"] is True
        assert tightened["rule_version"] == "2"
        assert tightened["interval_created"] is True
        assert len(store_mod.store.rows(ledger.INTERVAL_MODULE)) == 2

    def test_direct_threshold_edit_is_detected_as_new_version(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        rule["阈值上限"] = "9.0"
        result = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                 observed_at="2026-11-21 13:10", shift="午班")
        assert result["rule_version"] == "2"
        assert result["breached"] is False


class TestIssueWriteback:

    def test_first_issue_links_two_breaching_points_and_retest(self):
        q, _ = _fresh()
        rule = _temperature_rule(q)
        a = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                            observed_at="2026-11-21 06:10", shift="早班", message_id="M-early")
        b = q.execute_check(rule["id"], 9.2, point_code="TP-02",
                            observed_at="2026-11-21 06:40", shift="早班", message_id="M-early")
        c = q.execute_check(rule["id"], 8.2, point_code="TP-01",
                            observed_at="2026-11-21 12:10", shift="午班", message_id="M-noon")

        assert a["issue"]["id"] == b["issue"]["id"] == c["issue_id"]
        detail = q.get_issue_quality_detail(a["issue"]["id"])
        assert sorted(detail["越界测点"]) == ["TP-01", "TP-02"]
        assert detail["首次越界值"] == 8.7
        assert detail["最新复测值"] == 8.2
        assert detail["仍待复测区间数"] == 1
        assert detail["区间"][0]["越界次数"] == 2
        assert [item["测点编号"] for item in detail["区间"][0]["越界明细"]] == ["TP-01", "TP-02"]

    def test_fix_is_kept_and_new_breach_reopens_same_interval(self):
        q, issues = _fresh()
        rule = _temperature_rule(q)
        first = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                observed_at="2026-11-21 06:10", shift="早班")
        issue_id = first["issue"]["id"]
        fixed = issues.fix_issue(issue_id, "已更换温度探头")
        assert fixed["status"] == "已修复"

        again_one = q.execute_check(rule["id"], 9.1, point_code="TP-01",
                                    observed_at="2026-11-21 13:10", shift="午班")
        again_two = q.execute_check(rule["id"], 9.2, point_code="TP-01",
                                    observed_at="2026-11-21 13:20", shift="午班")

        assert again_one["interval_id"] == first["interval_id"]
        assert again_two["interval_id"] == first["interval_id"]
        assert again_one["issue_created"] is False
        assert again_one["issue_reopened"] is True
        assert len(store_mod.store.rows(ledger.INTERVAL_MODULE)) == 1
        issue = store_mod.store.find("issue_track", issue_id)
        assert issue["status"] == "处理中"
        assert issue["指派处理"] == "巡检员甲"
        assert issue["复测负责人"] == "巡检员甲"
        # 原有修复记录和首次越界值都还在。
        assert "已更换温度探头" in issue["修复方案"]
        assert issue["首次越界值"] == 8.7
        assert ledger.get_interval(first["interval_id"])["越界次数"] == 3

    def test_verified_issue_makes_next_breach_a_new_interval(self):
        q, issues = _fresh()
        rule = _temperature_rule(q)
        first = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                observed_at="2026-11-21 06:10", shift="早班")
        issues.fix_issue(first["issue"]["id"], "调整散热")
        issues.verify_issue(first["issue"]["id"], "巡检员甲")
        assert ledger.get_interval(first["interval_id"])["状态"] == "已复测通过"

        later = q.execute_check(rule["id"], 9.3, point_code="TP-01",
                                observed_at="2026-11-21 18:10", shift="晚班")
        assert later["interval_created"] is True
        assert later["interval_id"] != first["interval_id"]
        assert later["issue"]["id"] != first["issue"]["id"]

    def test_conclusion_writeback_appends_without_erasing_fix_record(self):
        q, issues = _fresh()
        rule = _temperature_rule(q)
        first = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                observed_at="2026-11-21 06:10", shift="早班")
        interval_id = first["interval_id"]
        issue_id = first["issue"]["id"]
        issues.fix_issue(issue_id, "更换探头")
        q.execute_check(rule["id"], 8.2, point_code="TP-01",
                        observed_at="2026-11-21 12:10", shift="午班")

        result = q.write_conclusion(interval_id, "复测通过，恢复正常监测", operator="巡检员甲", close=True)
        assert result["ok"] is True
        issue = store_mod.store.find("issue_track", issue_id)
        assert "更换探头" in issue["修复方案"]
        assert "复测通过，恢复正常监测" in issue["修复方案"]
        assert issue["status"] == "已验证"
        assert issue["首次越界值"] == 8.7
        assert issue["最新复测值"] == 8.2
        interval = ledger.get_interval(interval_id)
        assert interval["状态"] == "已复测通过"
        assert interval["质检结论"] == "复测通过，恢复正常监测"

    def test_open_interval_lookup_after_replay(self):
        """重复上报只更新既有观测，仍待复测的区间数量不增加。"""
        q, _ = _fresh()
        rule = _temperature_rule(q)
        q.execute_check(rule["id"], 9.2, point_code="TP-01",
                        observed_at="2026-11-21 06:40", shift="早班", message_id="M-early")
        q.execute_check(rule["id"], 9.2, point_code="TP-01",
                        observed_at="2026-11-21 06:40", shift="晚班", message_id="M-early")
        q.execute_check(rule["id"], 9.2, point_code="TP-01",
                        observed_at="2026-11-21 06:40", shift="夜班", message_id="M-early")
        open_intervals = ledger.list_intervals(rule_id=rule["id"], pending_only=True)
        assert len(open_intervals) == 1
        assert open_intervals[0]["越界次数"] == 1


class TestThresholdTenScenario:

    def test_report_eleven_twelve_nine_then_twelve_again(self):
        """阈值 10：上报 11、12、9，再上报 12，核对工单和仍待复测区间。"""
        q, issues = _fresh()
        rule = q.define_rule("温度测点B", "上限", "", 10, "阻断",
                             point_code="TP-10", inspector="巡检员乙", rule_code="QUAL-TEMP10")
        eleven = q.execute_check(rule["id"], 11, point_code="TP-10",
                                 observed_at="2026-11-21 08:00", shift="早班", message_id="M1")
        twelve = q.execute_check(rule["id"], 12, point_code="TP-10",
                                 observed_at="2026-11-21 09:00", shift="早班", message_id="M1")
        nine = q.execute_check(rule["id"], 9, point_code="TP-10",
                               observed_at="2026-11-21 10:00", shift="午班", message_id="M2")
        replay = q.execute_check(rule["id"], 12, point_code="TP-10",
                                 observed_at="2026-11-21 09:00", shift="夜班", message_id="M1")

        assert eleven["breached"] and twelve["breached"] and not nine["breached"]
        assert replay["duplicate"] is True
        assert eleven["interval_id"] == twelve["interval_id"] == nine["interval_id"]
        issue_id = eleven["issue"]["id"]

        detail = q.get_issue_quality_detail(issue_id)
        assert detail["工单编号"] == eleven["issue"]["问题编号"]
        assert detail["首次越界值"] == 11
        assert detail["区间"][0]["最新越界值"] == 12
        assert detail["区间"][0]["最新复测值"] == 9
        assert detail["区间"][0]["越界次数"] == 2
        assert detail["仍待复测区间数"] == 1
        open_issues = [item for item in issues.get_open_issues() if item["id"] == issue_id]
        assert len(open_issues) == 1
        assert open_issues[0]["status"] == "待确认"


class TestIssueHandlerRetrieval:

    def test_issue_track_exposes_quality_detail(self):
        q, issues = _fresh()
        rule = _temperature_rule(q)
        first = q.execute_check(rule["id"], 8.7, point_code="TP-01",
                                observed_at="2026-11-21 06:10", shift="早班")
        q.execute_check(rule["id"], 8.2, point_code="TP-01",
                        observed_at="2026-11-21 12:10", shift="午班")
        detail = issues.get_quality_detail(first["issue"]["id"])
        assert detail["首次越界值"] == 8.7
        assert detail["最新复测值"] == 8.2
        assert detail["处理人"] == "巡检员甲"
