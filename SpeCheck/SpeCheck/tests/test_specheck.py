import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import specheck


class BatteryReportTests(unittest.TestCase):
    def test_reads_capacities_from_windows_report_html(self):
        html = '''<table><tr><td><span class="label">DESIGN CAPACITY</span></td>
        <td><span class="value">58,003 mWh</span></td></tr>
        <tr><td><span class="label">FULL CHARGE CAPACITY</span></td>
        <td><span class="value">33,911 mWh</span></td></tr></table>'''
        values = specheck.parse_battery_report(html)
        self.assertEqual(values["designCapacity"], 58003)
        self.assertEqual(values["fullCapacity"], 33911)
        health = specheck.battery_health_percent(values["designCapacity"], values["fullCapacity"])
        self.assertAlmostEqual(health, 58.4642, places=3)
        self.assertEqual(f"{health:.1f}%", "58.5%")

    def test_missing_capacity_is_not_misreported_as_zero_percent(self):
        self.assertIsNone(specheck.battery_health_percent(None, None))


class ReadingFormatTests(unittest.TestCase):
    def test_memory_is_reported_in_gigabytes(self):
        self.assertEqual(specheck.memory_capacity_text(16 * 1024 * 1024), "16.0 GB")

    def test_storage_status_is_normalized(self):
        self.assertEqual(specheck.disk_status({"HealthStatus": "Healthy"}), "Healthy")
        self.assertEqual(specheck.disk_status({"HealthStatus": "Unknown"}), "Not reported")

    def test_storage_free_and_used_percent_use_volume_capacity(self):
        total = 100 * 1024**3
        result = specheck.storage_usage(total, 25 * 1024**3)
        self.assertEqual(result["total"], total)
        self.assertEqual(result["free"], 25 * 1024**3)
        self.assertEqual(result["usedPercent"], 75.0)

    def test_invalid_storage_values_are_not_displayed(self):
        self.assertIsNone(specheck.storage_usage(0, 0))
        self.assertIsNone(specheck.storage_usage(10, 11))

    def test_memory_summary_prefers_configured_speed_and_lists_modules(self):
        modules = [
            {"ConfiguredClockSpeed": 3200, "Speed": 3600},
            {"ConfiguredClockSpeed": 3200, "Speed": 3600},
        ]
        self.assertEqual(specheck.memory_module_summary(modules), "2 module(s) · 3200 MHz configured speed")
        self.assertIn("Samsung", specheck.memory_module_detail({"Manufacturer": "Samsung", "Capacity": 8 * 1024**3, "DeviceLocator": "DIMM 1", "Speed": 2666}, 1))

    def test_battery_cycle_count_shows_only_supported_positive_values(self):
        self.assertEqual(specheck.battery_cycle_count_text({"batteryCycleCounts": [{"CycleCount": 420}]}), "420 cycles")
        self.assertEqual(specheck.battery_cycle_count_text({"batteryCycleCounts": [{"CycleCount": 0}]}), "Not reported")
        self.assertEqual(specheck.battery_cycle_count_text({}), "Not reported")

    def test_physical_drive_type_and_model_are_normalized(self):
        disk = {"MediaType": "4", "FriendlyName": "NVMe Example", "HealthStatus": "Healthy", "BusType": "NVMe"}
        self.assertEqual(specheck.disk_type_text(disk), "SSD")
        self.assertEqual(specheck.disk_model_text(disk), "NVMe Example")
        self.assertIn("Windows health: Healthy", specheck.disk_spec_text(disk))

    def test_manufacturer_logo_matches_common_oems_and_aliases(self):
        self.assertEqual(specheck.device_logo_key("Dell Inc.", "Latitude 5430"), "dell")
        self.assertEqual(specheck.device_logo_key("HP", "EliteBook"), "hp")
        self.assertEqual(specheck.device_logo_key("Dell Inc.", "Alienware m16"), "alienware")


class QuickCheckTests(unittest.TestCase):
    def test_nvidia_adapter_is_selected_over_integrated_graphics(self):
        gpus = [
            {"Name": "Intel(R) Iris(R) Xe Graphics", "AdapterRAM": 1024},
            {"Name": "NVIDIA GeForce RTX 4060 Laptop GPU", "AdapterRAM": 8 * 1024**3},
        ]
        self.assertEqual(specheck.select_primary_gpu(gpus)["Name"], "NVIDIA GeForce RTX 4060 Laptop GPU")

    def test_windows_vram_field_is_not_presented_as_exact_above_4gb(self):
        self.assertIn("verify exact GPU spec", specheck.reported_vram_text(4 * 1024**3))
        self.assertEqual(specheck.reported_vram_text(2 * 1024**3), "2.0 GB reported adapter memory")

    def test_low_battery_and_drive_errors_raise_review_status(self):
        data = {
            "designCapacity": 100,
            "fullCapacity": 55,
            "disks": [{"FriendlyName": "NVMe", "HealthStatus": "Warning"}],
            "systemErrors": [{"Provider": "WHEA-Logger", "Count": 2}],
            "systemErrorsAvailable": True,
        }
        findings = specheck.quick_check_findings(data)
        self.assertEqual(len(findings), 3)
        self.assertEqual(specheck.quick_check_summary(data)[0], "REVIEW BEFORE BUYING")

    def test_missing_readings_are_reported_as_partial_check(self):
        self.assertEqual(specheck.quick_check_summary({})[0], "SOME CHECKS UNAVAILABLE")

    def test_complete_readings_without_flags_use_cautious_summary(self):
        data = {
            "designCapacity": 100,
            "fullCapacity": 90,
            "cpu": {"Name": "Example CPU"},
            "gpus": [{"Name": "Example GPU"}],
            "memoryTotalKB": 1024,
            "volumes": [{"DeviceID": "C:", "Size": 1000, "FreeSpace": 500}],
            "disks": [{"HealthStatus": "Healthy"}],
            "systemErrorsAvailable": True,
            "systemErrors": [],
        }
        self.assertEqual(specheck.quick_check_summary(data)[0], "NO HIGH-PRIORITY FLAGS")

    def test_event_counts_and_sources_are_summarized(self):
        events = [{"Provider": "Disk", "Count": 3}, {"Provider": "nvlddmkm", "Count": 2}]
        self.assertEqual(specheck.system_error_count(events), 5)
        self.assertEqual(specheck.system_error_sources(events), "disk, NVIDIA graphics driver")


if __name__ == "__main__":
    unittest.main()
