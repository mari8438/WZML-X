import os
import re
import time
from bottle import Bottle, request, response, run
import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

app = Bottle()


def get_driver():
    display = os.environ.get("DISPLAY")
    if not display:
        import glob
        x_sockets = glob.glob("/tmp/.X11-unix/X*")
        if x_sockets:
            display = ":" + x_sockets[0].split("/X")[-1]
            os.environ["DISPLAY"] = display

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,720")
    return uc.Chrome(options=options, driver_executable_path="/app/chromedriver")


@app.post("/solve")
def solve_endpoint():
    data = request.json or {}
    url = data.get("url")
    if not url:
        response.status = 400
        return {"error": "Missing url"}

    driver = None
    try:
        driver = get_driver()
        driver.get(url)
        time.sleep(4)

        # Check if already on the downloads page
        if "gdflix" in driver.page_source.lower():
            html = driver.page_source
            gd_match = re.search(r'href="([^"]*gdflix[^"]*)"', html, re.IGNORECASE)
            return {
                "status": "success",
                "title": driver.title,
                "gdflix_url": gd_match.group(1) if gd_match else None,
                "page_url": driver.current_url,
            }

        # Try to locate submitBtn, or wait up to 10s
        btn = None
        for _ in range(10):
            try:
                btn = driver.find_element(By.ID, "submitBtn")
                break
            except Exception:
                time.sleep(1)

        if btn:
            for sec in range(25):
                disabled = btn.get_attribute("disabled")
                if not disabled:
                    btn.click()
                    time.sleep(4)
                    break
                if sec in (2, 5, 8):
                    try:
                        box = driver.find_element(By.CLASS_NAME, "cf-turnstile")
                        ActionChains(driver).move_to_element(box).click().perform()
                    except Exception:
                        pass
                time.sleep(1)
        else:
            time.sleep(5)

        html = driver.page_source
        title = driver.title

        # Look specifically for GDFlix links
        gd_match = re.search(r'href="([^"]*gdflix[^"]*)"', html, re.IGNORECASE)
        gdflix_url = gd_match.group(1) if gd_match else None

        # Fallback to other mirror hosts if needed
        all_links = {}
        for h, t in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
            ct = re.sub(r'<[^>]+>', ' ', t).strip()
            if any(k in h.lower() for k in ["gdflix", "filebee", "pixeldrain", "mega", "gdshare"]):
                all_links[ct] = h
                if not gdflix_url and "gdflix" in h.lower():
                    gdflix_url = h

        return {
            "status": "success",
            "title": title,
            "gdflix_url": gdflix_url,
            "mirrors": all_links,
            "page_url": driver.current_url,
        }
    except Exception as err:
        response.status = 500
        return {"status": "error", "error": str(err)}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


@app.get("/health")
def health():
    return {"status": "ok", "service": "HAZ Turnstile Solver"}


if __name__ == "__main__":
    run(app, host="0.0.0.0", port=8192)
