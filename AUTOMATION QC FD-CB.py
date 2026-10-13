import time
import random
import requests
import gspread
import pandas as pd
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import customtkinter as ctk
import threading

# ==========================================
# 1. CẤU HÌNH & DICTIONARY TOÀN CỤC
# ==========================================
STATUS_MAP = {
    '0': 'Pending', '1': 'Scanning', '2': 'Received', '3': 'Putting', 
    '4': 'Done', '5': 'Cancelled', '6': 'Arrived', '9': 'Checking', 
    '10': 'Checked', '13': 'In Transit', '17': 'Dispute', '18': 'Judging', 
    '19': 'Judged', '100': 'Completed'
}

SHEET_KEY = '1zUvOI_9iARGQgf2EFfedZLPnFCMTQzEUVcCqDHwS6Vw'
CREDENTIALS_FILE = 'vns-rt-ft-103.json'
URL_WMS = 'https://wms.ssc.shopee.vn/v2/returninbound/arrving'

driver = None
api_session = None
active_login_config = None
is_running = False
stop_requested = False

# ==========================================
# 2. KẾT NỐI GOOGLE SHEETS
# ==========================================
try:
    gd = gspread.service_account(filename=CREDENTIALS_FILE)
    gs = gd.open_by_key(SHEET_KEY)
    ws2 = gs.worksheet('MASS QC TAY')
    ws3 = gs.worksheet('MASS QC TAY')
except Exception as e:
    print(f"Lỗi khởi tạo Google Sheets: {e}")

# ==========================================
# 3. ĐĂNG NHẬP WMS & TẠO API SESSION
# ==========================================
def init_driver_and_login(log_callback, update_status_callback, email, password, warehouse_code):
    global driver, api_session
    update_status_callback("Đang khởi động...", "warning")
    log_callback("Khởi tạo Chrome ở chế độ ngầm (Headless)...", "info")

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 80)

    try:
        update_status_callback("Đang đăng nhập WMS...", "warning")
        log_callback("Mở trang đăng nhập Shopee WMS...", "info")
        driver.get(URL_WMS)

        user_name = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="warehouse-management-system-authKey"]')))
        user_name.clear()
        user_name.send_keys(email)
        time.sleep(1)

        pass_word = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="warehouse-management-system-password"]')))
        pass_word.clear()
        pass_word.send_keys(password)
        time.sleep(1)
        pass_word.send_keys(Keys.ENTER)

        log_callback("Dang cho xac nhan dang nhap...", "info")
        wait.until(EC.presence_of_element_located((By.XPATH, '//section')))
        log_callback("Dang nhap thanh cong, dang chon Station kho...", "success")
        log_callback("Đang chọn Station kho...", "info")
        wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="app"]/div[1]/div[1]/section[1]/span/span[1]/div/div/span[1]/span/input'))).click()
        time.sleep(1.5)
        driver.find_element(By.XPATH, f'/html/body/span/div/div/div/ul/li[{warehouse_code}]/span').click()
        time.sleep(5)

        api_session = requests.Session()
        for cookie in driver.get_cookies():
            api_session.cookies.set(cookie['name'], cookie['value'])

        api_session.headers.update({
            "User-Agent": driver.execute_script("return navigator.userAgent;"),
            "Content-Type": "application/json",
            "Referer": URL_WMS,
            "x-cctv-tenant-id": "WMS"
        })
        log_callback("Đăng nhập thành công! API Session đã sẵn sàng.", "success")
        update_status_callback("Sẵn sàng hoạt động", "normal")
        return True
    except Exception as e:
        log_callback(f"Lỗi đăng nhập: {e}", "error")
        update_status_callback("Lỗi đăng nhập!", "error")
        return False

# ==========================================
# 4. HÀM CẬP NHẬT SHEET
# ==========================================
def mark_as_error(return_tn, reason=""):
    # Ghi mã đơn ở cột G và nội dung lỗi ở cột H của sheet MASS QC TAY.
    ws3.append_row([str(return_tn).strip(), f'error: {reason}'], table_range='G1:H1')

def mark_as_done(return_tn):
    # Chỉ ghi lỗi vào G:H; đơn thành công không cần thêm dòng log.
    pass

# ==========================================
# 5. HÀM TRA CỨU TRẠNG THÁI & INBOUND_ID (RIVNBD...)
# ==========================================
def get_order_status_and_inbound(sheet_code):
    # 1. Tra cứu qua API Search ASN
    try:
        search_url = f"https://wms.ssc.shopee.vn/api/apps/process/returninbound/riasn/search_asn?sheet_id={sheet_code}&sort_field=&sort_type=1&pageno=1&count=20"
        res = api_session.get(search_url).json()
        data = res.get("data") or {}
        item_list = data.get("list") or []
        if item_list:
            info = item_list[0]
            inb = info.get("inbound_id") or info.get("asn")
            status = str(info.get("task_status"))
            ret_tn = info.get("return_tn", sheet_code)
            return status, inb, ret_tn
    except Exception:
        pass

    # 2. Tra cứu qua API Judge Scan Sheet
    try:
        scan_judge_url = "https://wms.ssc.shopee.vn/api/apps/process/returninbound/judge/scan_sheet_id"
        res_j = api_session.post(scan_judge_url, json={"count": 20, "pageno": 1, "sheet_id": sheet_code}).json()
        data_j = res_j.get("data") or {}
        inb = data_j.get("inbound_id")
        if res_j.get("retcode") == 0 and inb:
            return '18', inb, sheet_code
    except Exception:
        pass

    return None, None, sheet_code

# ==========================================
# 6. CÁC MODULE XỬ LÝ API
# ==========================================
def execute_arriving(return_tn, app_log):
    app_log(f"[{return_tn}] Đang thực hiện Arriving...", "info")
    scan_url = "https://wms.ssc.shopee.vn/api/apps/process/returninbound/arrival/scan_sheet_id"
    complete_url = "https://wms.ssc.shopee.vn/api/apps/process/returninbound/arrival/create_accept_reject_arrival"

    scan_payload = {"sheet_id": str(return_tn).strip()}
    resp_scan = api_session.post(scan_url, json=scan_payload).json()

    retmsg = resp_scan.get("message", "")
    if "not in pending/In transit status" in retmsg:
        app_log(f"[{return_tn}] Đơn đã qua bước Arriving. Chuyển QC/Judge...", "warning")
        return "ALREADY_ARRIVED"

    data_scan = resp_scan.get("data") or {}
    asn_list = data_scan.get("list") or []

    if resp_scan.get("retcode") != 0 or not asn_list:
        app_log(f"[{return_tn}] Lỗi Scan Arriving: {retmsg}", "error")
        return False

    return_channel_id = asn_list[0].get("return_channel_id", "50030") if asn_list else "50030"

    for item in asn_list:
        item["arrival_action"] = 1
        item["follow_up"] = item.get("suggest_follow_up")

    complete_payload = {
        "arrival_remark": "",
        "arriving_handover_form": 0,
        "asn_list": asn_list,
        "dock_no_list": [],
        "is_group": 0,
        "license_plate_num_list": [],
        "return_channel_id": str(return_channel_id),
        "transporter_name_list": []
    }

    resp_complete = api_session.post(complete_url, json=complete_payload).json()
    if resp_complete.get("retcode") == 0:
        app_log(f"[{return_tn}] Arriving thành công!", "info")
        return True
    else:
        app_log(f"[{return_tn}] Lỗi Complete Arriving: {resp_complete.get('message')}", "error")
        return False

def execute_qc(return_tn, app_log):
    app_log(f"[{return_tn}] Đang thực hiện QC Ảo Fail Delivery...", "info")
    
    qc_scan_url = f"https://wms.ssc.shopee.vn/api/apps/process/returninbound/riqc/scan_sheet_id?sheet_id={return_tn}&pageno=1&count=9999"
    res_qc_scan = api_session.get(qc_scan_url).json()
    
    retmsg = res_qc_scan.get("message", "")
    data_qc = res_qc_scan.get("data") or {}
    qc_list = data_qc.get("list") or []

    # Nếu đơn đã qua bước QC, tra cứu lấy lại Inbound ID (RIVNBD...) để truyền cho Judging
    if "should be arrived and checking" in retmsg or "not in" in retmsg or "arrived" in retmsg.lower():
        app_log(f"[{return_tn}] Đã qua bước QC. Đang tra cứu Inbound ID...", "warning")
        _, inb, _ = get_order_status_and_inbound(return_tn)
        return True, inb

    if res_qc_scan.get("retcode") != 0 or not qc_list:
        app_log(f"[{return_tn}] Lỗi QC Scan Sheet: {retmsg}", "error")
        return False, None

    asn = qc_list[0].get("asn")

    search_asn_url = f"https://wms.ssc.shopee.vn/api/apps/process/returninbound/riqc/search_asn?asn={asn}"
    res_asn = api_session.get(search_asn_url).json()
    data_asn = res_asn.get("data") or {}

    if res_asn.get("retcode") != 0:
        app_log(f"[{return_tn}] Lỗi Search ASN: {res_asn.get('message')}", "error")
        return False, None

    task_id = data_asn.get("task_id")

    if not task_id:
        app_log(f"[{return_tn}] Không tìm thấy Task ID QC", "error")
        return False, None

    save_parcel_payload = {
        "task_id": task_id,
        "parcel_quality": 0,
        "remark": "",
        "return_qc_images": [],
        "return_qc_videos": []
    }
    
    save_res = api_session.post(
        "https://wms.ssc.shopee.vn/api/apps/process/returninbound/riqc/save_parcel_qc", 
        json=save_parcel_payload
    ).json()

    if save_res.get("retcode") == 0:
        inbound_id = asn  
        app_log(f"[{return_tn}] QC Parcel Hoàn tất -> Inbound ID: {inbound_id}", "info")
        return True, inbound_id
    else:
        app_log(f"[{return_tn}] Lỗi Save Parcel QC: {save_res.get('message')}", "error")
        return False, None

def execute_judge(inbound_id, return_tn, app_log):
    # 1. BẮT BUỘC KÍCH HOẠT SESSION BẰNG API scan_sheet_id
    scan_judge_url = "https://wms.ssc.shopee.vn/api/apps/process/returninbound/judge/scan_sheet_id"
    res_scan = api_session.post(scan_judge_url, json={"count": 20, "pageno": 1, "sheet_id": return_tn}).json()
    data_scan = res_scan.get("data") or {}
    
    # Ưu tiên lấy Inbound ID chuẩn từ phản hồi của scan_sheet_id
    real_inbound_id = data_scan.get("inbound_id") or inbound_id

    if not real_inbound_id or not str(real_inbound_id).startswith("RIVNBD"):
        app_log(f"[{return_tn}] Lỗi: Hệ thống không tạo được Inbound ID (RIVNBD...) để Judging!", "error")
        return False

    app_log(f"[{return_tn}] Đang thực hiện Judging cho Inbound: {real_inbound_id}...", "info")
    
    # 2. Lấy chi tiết đơn hàng (get_parcel_detail)
    detail_url = f"https://wms.ssc.shopee.vn/api/apps/process/returninbound/judge/get_parcel_detail?inbound_id={real_inbound_id}"
    res_detail = api_session.get(detail_url).json()
    data_detail = res_detail.get("data") or {}
    detail = data_detail.get("item") or {}

    # 3. Lấy Working List
    judge_list_url = f"https://wms.ssc.shopee.vn/api/apps/process/returninbound/judge/get_working_list?inbound_id={real_inbound_id}&pageno=1&count=20"
    judge_res = api_session.get(judge_list_url).json()
    data_judge = judge_res.get("data") or {}
    judge_items = data_judge.get("list") or []
    
    total_price = "0.00"
    if judge_items:
        total_price = judge_items[0].get("item_price", "0.00")

    # 4. Lấy gợi ý hệ thống (Suggest Follow Up)
    suggest_url = "https://wms.ssc.shopee.vn/api/apps/process/returninbound/judge/get_suggest_follow_up"
    suggest_payload = {
        "channel_integrated_type": detail.get("channel_integrated_type", 1),
        "damage_type": detail.get("damage_type", 0),
        "inbound_id": real_inbound_id,
        "is_abnormal_sku": False,
        "is_cb": True,
        "is_sbs": False,
        "is_shop_disable_rts": detail.get("is_shop_disable_rts", 2),
        "is_shortage": False,
        "item_price": "",
        "judge_result": 1,
        "parcel_threshold_value": detail.get("parcel_threshold_value", "450000"),
        "return_reason": detail.get("return_reason", 6),
        "return_status": 0,
        "seller_region": detail.get("seller_region", "CN"),
        "sku_quality": detail.get("parcel_quality", 0),
        "total_price": str(total_price)
    }
    res_suggest = api_session.post(suggest_url, json=suggest_payload).json()
    data_suggest = res_suggest.get("data") or {}
    follow_up_val = data_suggest.get("suggest_follow_up", 8)

    # 5. Xác nhận Judging với confirm_parcel_judge
    confirm_url = "https://wms.ssc.shopee.vn/api/apps/process/returninbound/judge/confirm_parcel_judge"
    confirm_payload = {
        "inbound_id": real_inbound_id,
        "claim_images": [],
        "need_3pl_claim": 0,
        "suggest_follow_up": follow_up_val,
        "follow_up": follow_up_val,
        "follow_up_remark": ""
    }
    
    cj_res = api_session.post(confirm_url, json=confirm_payload).json()

    if cj_res.get("retcode") == 0:
        app_log(f"[{return_tn}] Confirm Parcel Judge thành công (Follow Up: {follow_up_val})!", "success")
        return True
    else:
        app_log(f"[{return_tn}] Lỗi Confirm Parcel Judge: {cj_res.get('message')}", "error")
        return False

# ==========================================
# 7. ROUTER ĐIỀU HƯỚNG TỰ ĐỘNG
# ==========================================
def process_single_order_auto(sheet_code, app_log):
    task_status, inbound_id, actual_return_tn = get_order_status_and_inbound(sheet_code)

    # Trường hợp không tra được status/Inbound ID -> Cho chạy tuần tự Arriving & QC
    if not task_status or not inbound_id or not str(inbound_id).startswith("RIVNBD"):
        app_log(f"[{sheet_code}] Đơn mới/Chưa có Inbound ID -> Chạy tuần tự Arriving & QC", "warning")
        arrive_res = execute_arriving(actual_return_tn, app_log)
        if arrive_res is False: return False
        
        qc_ok, inbound_id_new = execute_qc(actual_return_tn, app_log)
        if not qc_ok: return False
        
        target_inbound = inbound_id_new if inbound_id_new else inbound_id
        return execute_judge(target_inbound, actual_return_tn, app_log)

    status_label = STATUS_MAP.get(task_status, f"Unknown({task_status})")

    # 1. Chưa Arrive -> Arrive -> QC -> Judge
    if task_status in ['0', '13']:
        app_log(f"[{actual_return_tn}] Status: {status_label} ({task_status}) -> Chạy Arriving", "warning")
        arrive_res = execute_arriving(actual_return_tn, app_log)
        if arrive_res is False: return False
        
        qc_ok, inbound_id_new = execute_qc(actual_return_tn, app_log)
        if not qc_ok: return False
        return execute_judge(inbound_id_new if inbound_id_new else inbound_id, actual_return_tn, app_log)

    # 2. Đã Arrive hoặc đang QC -> Nhảy vào QC & Judge
    elif task_status in ['1', '2', '6', '9', '12']:
        app_log(f"[{actual_return_tn}] Status: {status_label} ({task_status}) -> Bắt đầu QC & Judge", "warning")
        qc_ok, inbound_id_new = execute_qc(actual_return_tn, app_log)
        if not qc_ok: return False
        return execute_judge(inbound_id_new if inbound_id_new else inbound_id, actual_return_tn, app_log)

    # 3. Đã QC xong / Đang Judging -> Nhảy trực tiếp tới Judge
    elif task_status in ['10', '18']:
        app_log(f"[{actual_return_tn}] Status: {status_label} ({task_status}) -> Nhảy trực tiếp tới Judging", "warning")
        return execute_judge(inbound_id, actual_return_tn, app_log)

    # 4. Đã hoàn thành từ trước
    elif task_status in ['3', '4', '19', '100']:
        app_log(f"[{actual_return_tn}] Status: {status_label} ({task_status}) -> Đã HOÀN THÀNH từ trước!", "success")
        return True

    # 5. Đơn bị hủy / tranh chấp
    elif task_status in ['5', '17']:
        app_log(f"[{actual_return_tn}] Status: {status_label} ({task_status}) -> Bị hủy/tranh chấp, bỏ qua", "error")
        return False

    else:
        app_log(f"[{actual_return_tn}] Status: {status_label} ({task_status}) -> Chuyển trực tiếp tới Judging", "warning")
        return execute_judge(inbound_id, actual_return_tn, app_log)

# ==========================================
# 8. QUẢN LÝ TIẾN TRÌNH THỰC THI
# ==========================================
def process_returns(app, login_config):
    global is_running, stop_requested, driver, api_session, active_login_config

    if not api_session or active_login_config != login_config:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            driver = None
        api_session = None
        if not init_driver_and_login(app.add_log, app.update_status, *login_config):
            is_running = False
            app.set_ui_state(running=False)
            return
        active_login_config = login_config

    app.add_log("Đang truy vấn dữ liệu từ Google Sheets...", "info")
    try:
        get_all_value = ws2.get('A2:A')
        orders = [row[0].strip() for row in get_all_value if row and len(row) > 0 and str(row[0]).strip() != ""]
    except Exception as e:
        app.add_log(f"Lỗi truy vấn Google Sheets: {e}", "error")
        is_running = False
        app.set_ui_state(running=False)
        return

    total_orders = len(orders)
    if total_orders == 0:
        app.add_log("Không tìm thấy đơn hàng cần xử lý.", "warning")
        is_running = False
        app.set_ui_state(running=False)
        return

    app.add_log(f"Phát hiện {total_orders} đơn hàng cần kiểm tra", "info")
    app.update_metrics(total=total_orders, success=0, error=0)

    success_cnt = 0
    error_cnt = 0

    for idx, sheet_code in enumerate(orders, 1):
        if stop_requested:
            app.add_log("Tiến trình đã hủy theo yêu cầu người dùng!", "warning")
            break

        app.update_progress(idx, total_orders)
        app.update_status(f"[{idx}/{total_orders}] Xử lý: {sheet_code}", "running")

        try:
            status_ok = process_single_order_auto(sheet_code, app.add_log)
            if status_ok:
                mark_as_done(sheet_code)
                success_cnt += 1
                app.update_metrics(success=success_cnt)
            else:
                mark_as_error(sheet_code, "Xử lý thất bại")
                error_cnt += 1
                app.update_metrics(error=error_cnt)

            time.sleep(random.uniform(1.0, 1.8))

        except Exception as e:
            app.add_log(f"[{sheet_code}] Lỗi ngoại lệ: {e}", "error")
            mark_as_error(sheet_code, str(e))
            error_cnt += 1
            app.update_metrics(error=error_cnt)

    app.add_log(f"Xử lý hoàn tất! Thành công: {success_cnt} | Lỗi: {error_cnt}", "success")
    app.update_status("Sẵn sàng", "normal")
    is_running = False
    app.set_ui_state(running=False)

def run_loop_32(app, login_config):
    global is_running, stop_requested
    for i in range(32):
        if stop_requested:
            break
        app.add_log(f"=== KHỞI ĐỘNG CHU KỲ {i+1}/32 ===", "info")
        process_returns(app, login_config)
        
        if i < 31 and not stop_requested:
            app.add_log("Chờ 15 phút đến chu kỳ tiếp theo...", "warning")
            for sec in range(15 * 60, 0, -1):
                if stop_requested:
                    break
                if sec % 60 == 0:
                    app.update_status(f"Chờ chu kỳ {i+2}: {sec//60}m", "warning")
                time.sleep(1)
                
    is_running = False
    app.set_ui_state(running=False)

# ==========================================
# 9. GIAO DIỆN PREMIUM MODERN DARK UI (CUSTOMTKINTER)
# ==========================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("WMS AUTOMATION QC FD-CB TOOL BY PHONG GITHUB")
        self.geometry("780x700")
        self.resizable(False, False)
        
        self.BG_MAIN = "#0F1017"       
        self.BG_CARD = "#171925"       
        self.BG_CONSOLE = "#0B0C10"    
        self.COLOR_ACCENT = "#6366F1"  
        self.COLOR_SUCCESS = "#10B981" 
        self.COLOR_DANGER = "#EF4444"  
        self.TEXT_PRIMARY = "#F3F4F6"  
        self.TEXT_MUTED = "#9CA3AF"    

        self.configure(fg_color=self.BG_MAIN)
        ctk.set_appearance_mode("Dark")

        # HEADER SECTION
        self.header_frame = ctk.CTkFrame(self, corner_radius=12, fg_color=self.BG_CARD, border_width=1, border_color="#26293B")
        self.header_frame.pack(fill="x", padx=20, pady=(20, 12))

        self.title_label = ctk.CTkLabel(
            self.header_frame, 
            text="⚡ FD-CB QC TAY TỰ ĐỘNG API", 
            font=("Segoe UI", 18, "bold"), 
            text_color=self.TEXT_PRIMARY
        )
        self.title_label.pack(side="left", padx=18, pady=16)

        self.status_badge = ctk.CTkLabel(
            self.header_frame, 
            text="Sẵn sàng", 
            font=("Segoe UI", 11, "bold"), 
            fg_color="#26293B", 
            text_color="#A5B4FC", 
            corner_radius=20,
            padx=14, pady=4
        )
        self.status_badge.pack(side="right", padx=18)

        # LOGIN CONFIGURATION
        self.login_frame = ctk.CTkFrame(self, corner_radius=12, fg_color=self.BG_CARD, border_width=1, border_color="#26293B")
        self.login_frame.pack(fill="x", padx=20, pady=(0, 12))

        self.username_entry = ctk.CTkEntry(self.login_frame, placeholder_text="Tai khoan WMS", height=36)
        self.username_entry.pack(side="left", expand=True, fill="x", padx=(12, 6), pady=10)

        self.password_entry = ctk.CTkEntry(self.login_frame, placeholder_text="Mat khau WMS", show="*", height=36)
        self.password_entry.pack(side="left", expand=True, fill="x", padx=6, pady=10)

        self.warehouse_menu = ctk.CTkOptionMenu(self.login_frame, values=["1", "2", "3", "4"], height=36, width=110)
        self.warehouse_menu.set("1")
        self.warehouse_menu.pack(side="left", padx=(6, 12), pady=10)
        # METRICS SECTION
        self.metrics_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.metrics_frame.pack(fill="x", padx=20, pady=(0, 10))

        self.card_total = self.create_metric_card(self.metrics_frame, "TỔNG ĐƠN HÀNG", "0", "#1E2235", "#818CF8")
        self.card_total.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self.card_success = self.create_metric_card(self.metrics_frame, "THÀNH CÔNG", "0", "#122B22", "#34D399")
        self.card_success.pack(side="left", expand=True, fill="x", padx=6)

        self.card_error = self.create_metric_card(self.metrics_frame, "THẤT BẠI", "0", "#321A22", "#F87171")
        self.card_error.pack(side="left", expand=True, fill="x", padx=(6, 0))

        # PROGRESS BAR
        self.progress_bar = ctk.CTkProgressBar(self, height=6, corner_radius=3, fg_color="#1E2235", progress_color=self.COLOR_ACCENT)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", padx=20, pady=(0, 12))

        # CONSOLE LOG SECTION
        self.log_frame = ctk.CTkFrame(self, corner_radius=12, fg_color=self.BG_CARD, border_width=1, border_color="#26293B")
        self.log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        self.log_title = ctk.CTkLabel(self.log_frame, text="SYSTEM CONSOLE LOG", font=("Segoe UI", 10, "bold"), text_color=self.TEXT_MUTED)
        self.log_title.pack(anchor="w", padx=14, pady=(8, 0))

        self.log_box = ctk.CTkTextbox(self.log_frame, font=("Consolas", 11), text_color="#E5E7EB", fg_color=self.BG_CONSOLE, wrap="word", corner_radius=8)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.log_box.tag_config("info", foreground="#60A5FA")     
        self.log_box.tag_config("success", foreground="#34D399")  
        self.log_box.tag_config("error", foreground="#F87171")    
        self.log_box.tag_config("warning", foreground="#FBBF24")  

        # ACTION BUTTONS
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(fill="x", padx=20, pady=(0, 20))

        self.btn_run_once = ctk.CTkButton(
            self.btn_frame, text="▶  Quét 1 Lần", font=("Segoe UI", 12, "bold"), 
            fg_color=self.COLOR_ACCENT, hover_color="#4F46E5", text_color="#FFFFFF", 
            height=42, corner_radius=8, command=self.start_once
        )
        self.btn_run_once.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self.btn_run_loop = ctk.CTkButton(
            self.btn_frame, text="🔁  Chạy Vòng Lặp (32x)", font=("Segoe UI", 12, "bold"), 
            fg_color=self.COLOR_SUCCESS, hover_color="#059669", text_color="#FFFFFF", 
            height=42, corner_radius=8, command=self.start_loop
        )
        self.btn_run_loop.pack(side="left", expand=True, fill="x", padx=6)

        self.btn_stop = ctk.CTkButton(
            self.btn_frame, text="⏹  Dừng Tiến Trình", font=("Segoe UI", 12, "bold"), 
            fg_color=self.COLOR_DANGER, hover_color="#DC2626", text_color="#FFFFFF", 
            height=42, corner_radius=8, state="disabled", command=self.stop_process
        )
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(6, 0))

    def create_metric_card(self, parent, title, value, bg_color, text_color):
        card = ctk.CTkFrame(parent, fg_color=bg_color, corner_radius=10, border_width=1, border_color="#26293B")
        lbl_title = ctk.CTkLabel(card, text=title, font=("Segoe UI", 9, "bold"), text_color=self.TEXT_MUTED)
        lbl_title.pack(pady=(10, 0))
        lbl_val = ctk.CTkLabel(card, text=value, font=("Segoe UI", 20, "bold"), text_color=text_color)
        lbl_val.pack(pady=(0, 10))
        card.lbl_val = lbl_val
        return card

    def add_log(self, text, tag="info"):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{now}] {text}\n", tag)
        self.log_box.see("end")

    def update_status(self, text, mode="normal"):
        color_map = {
            "normal": ("#26293B", "#A5B4FC"),
            "running": ("#1E3A8A", "#93C5FD"),
            "warning": ("#451A03", "#FDE047"),
            "error": ("#451212", "#FCA5A5")
        }
        bg, fg = color_map.get(mode, color_map["normal"])
        self.status_badge.configure(text=text, fg_color=bg, text_color=fg)

    def update_metrics(self, total=None, success=None, error=None):
        if total is not None: self.card_total.lbl_val.configure(text=str(total))
        if success is not None: self.card_success.lbl_val.configure(text=str(success))
        if error is not None: self.card_error.lbl_val.configure(text=str(error))

    def update_progress(self, current, total):
        self.progress_bar.set(current / total)

    def set_ui_state(self, running=True):
        if running:
            self.btn_run_once.configure(state="disabled")
            self.btn_run_loop.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.username_entry.configure(state="disabled")
            self.password_entry.configure(state="disabled")
            self.warehouse_menu.configure(state="disabled")
        else:
            self.btn_run_once.configure(state="normal")
            self.btn_run_loop.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.username_entry.configure(state="normal")
            self.password_entry.configure(state="normal")
            self.warehouse_menu.configure(state="normal")


    def get_login_config(self):
        email = self.username_entry.get().strip()
        password = self.password_entry.get()
        warehouse_code = self.warehouse_menu.get()
        if not email or not password:
            self.add_log("Nhap tai khoan va mat khau WMS truoc khi chay.", "error")
            self.update_status("Thieu thong tin dang nhap", "error")
            return None
        if warehouse_code not in {"1", "2", "3", "4"}:
            self.add_log("Ma kho phai la 1, 2, 3 hoac 4.", "error")
            self.update_status("Ma kho khong hop le", "error")
            return None
        return email, password, warehouse_code
    def start_once(self):
        global is_running, stop_requested
        login_config = self.get_login_config()
        if login_config is None: return
        is_running = True
        stop_requested = False
        self.set_ui_state(running=True)
        threading.Thread(target=process_returns, args=(self, login_config), daemon=True).start()

    def start_loop(self):
        global is_running, stop_requested
        login_config = self.get_login_config()
        if login_config is None: return
        is_running = True
        stop_requested = False
        self.set_ui_state(running=True)
        threading.Thread(target=run_loop_32, args=(self, login_config), daemon=True).start()

    def stop_process(self):
        global stop_requested
        stop_requested = True
        self.add_log("Gửi yêu cầu dừng hệ thống...", "warning")

    def on_closing(self):
        global driver
        if driver:
            try: driver.quit()
            except: pass
        self.destroy()

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()