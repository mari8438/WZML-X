import glob
import os
import re
import time
from bottle import Bottle, request, response, run
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

# Import FlareSolverr's internal patched webdriver factory
from utils import get_webdriver

app = Bottle()


def get_driver():
    # Ensure Xvfb display is configured
    display = os.environ.get("DISPLAY")
    if not display:
        x_sockets = glob.glob("/tmp/.X11-unix/X*")
        if x_sockets:
            display = ":" + x_sockets[-1].split("/X")[-1]
            os.environ["DISPLAY"] = display

    # Cloudflare Turnstile strictly requires headful browser on Xvfb
    os.environ["HEADLESS"] = "false"
    return get_webdriver()


def extract_mirrors_and_best(html: str):
    mirrors = {}
    gdshare_url = None
    filepress_url = None
    gdflix_url = None

    for h, t in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
        ct = re.sub(r'<[^>]+>', ' ', t).strip()
        hl = h.lower()
        if not h or h.startswith("#") or "download1.php" in hl:
            continue
        # Exclude pixeldrain completely as requested by user
        if "pixeldrain" in hl or "pixeldrain" in ct.lower():
            continue

        if any(k in hl for k in ["gdshare", "gcloud"]):
            mirrors["gdshare"] = h
            if not gdshare_url:
                gdshare_url = h
        elif any(k in hl for k in ["filebee", "filepress", "press"]):
            mirrors["filepress"] = h
            if not filepress_url:
                filepress_url = h
        elif "gdflix" in hl or "gdflix" in ct.lower():
            mirrors["gdflix"] = h
            if not gdflix_url:
                gdflix_url = h
        elif "mega" in hl:
            mirrors["mega"] = h

    # Strict priority: GDShare -> FileBee/FilePress -> GDFlix
    best = gdshare_url or filepress_url or gdflix_url
    if not best and mirrors:
        for u in mirrors.values():
            if any(k in u.lower() for k in ["gdshare", "gcloud", "filebee", "filepress", "gdflix"]):
                best = u
                break

    return best, mirrors


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
        time.sleep(3)

        # 1. Check if already on downloads page
        best_u, m_dict = extract_mirrors_and_best(driver.page_source)
        if best_u:
            return {
                "status": "success",
                "title": driver.title,
                "gdflix_url": best_u,
                "mirrors": m_dict,
                "page_url": driver.current_url,
            }

        # 2. Handle Turnstile verification on download1.php
        btn = None
        for _ in range(8):
            try:
                btn = driver.find_element(By.ID, "submitBtn")
                break
            except Exception:
                time.sleep(1)

        if btn:
            for sec in range(20):
                is_disabled = btn.get_attribute("disabled")
                resp = driver.execute_script(
                    "return document.querySelector('[name=\"cf-turnstile-response\"]')?.value || window.turnstile?.getResponse();"
                )
                if not is_disabled or resp:
                    btn.click()
                    time.sleep(4)
                    break

                # Click turnstile box to trigger challenge
                if sec in (2, 5, 8, 12):
                    try:
                        box = driver.find_element(By.CLASS_NAME, "cf-turnstile")
                        ActionChains(driver).move_to_element(box).click().perform()
                    except Exception:
                        pass
                time.sleep(1)
        else:
            time.sleep(5)

        # 3. Extract mirrors from resolved downloads page
        html = driver.page_source
        title = driver.title
        best_url, mirrors = extract_mirrors_and_best(html)

        return {
            "status": "success",
            "title": title,
            "gdflix_url": best_url,
            "mirrors": mirrors,
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
