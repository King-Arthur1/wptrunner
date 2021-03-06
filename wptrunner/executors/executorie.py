# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import socket
import sys
import threading
import time
import traceback
import urlparse
import uuid
from collections import defaultdict

from .base import TestExecutor, testharness_result_converter, reftest_result_converter
from ..testrunner import Stop
from ..imagecomparer import ImageComparer

here = os.path.join(os.path.split(__file__)[0])

webdriver = None
exceptions = None

required_files = [("testharness_runner.html", "", False),
                  ("testharnessreport.js", "resources/", True)]


def do_delayed_imports():
    global webdriver
    global exceptions
    from selenium import webdriver
    from selenium.common import exceptions


class IETestExecutor(TestExecutor):
    def __init__(self, browser, http_server_url, timeout_multiplier=1, **kwargs):
        do_delayed_imports()
        TestExecutor.__init__(self, browser, http_server_url, timeout_multiplier)
        self.webdriver_port = browser.webdriver_port
        self.webdriver = None

        self.timer = None
        self.window_id = str(uuid.uuid4())
        self.capabilities = kwargs.pop("capabilities")

    def setup(self, runner, **kwargs):
        """Connect to browser via Selenium's WebDriver implementation."""
        self.runner = runner
        url = "http://localhost:%i/wd/url" % self.webdriver_port
        self.logger.debug("Connecting to Selenium on URL: %s" % url)

        session_started = False
        try:
            time.sleep(1)
            self.webdriver = webdriver.Ie(port=self.webdriver_port)
            time.sleep(10)
        except:
            self.logger.warning(
                "Connecting to Selenium failed:\n%s" % traceback.format_exc())
            time.sleep(1)
        else:
            self.logger.debug("Selenium session started")
            session_started = True

        if not session_started:
            self.logger.warning("Failed to connect to Selenium")
            self.runner.send_message("init_failed")
        else:
            try:
                self.after_connect()
            except:
                print >> sys.stderr, traceback.format_exc()
                self.logger.warning(
                    "Failed to connect to navigate initial page")
                self.runner.send_message("init_failed")
            else:
                self.runner.send_message("init_succeeded")

    def teardown(self):
        try:
            self.webdriver.quit()
        except:
            pass
        del self.webdriver

    def is_alive(self):
        try:
            # Get a simple property over the connection
            self.webdriver.current_window_handle
        # TODO what exception?
        except (socket.timeout, exceptions.ErrorInResponseException):
            return False
        return True

    def after_connect(self):
        url = urlparse.urljoin(self.http_server_url, "/testharness_runner.html")
        self.logger.debug("Loading %s" % url)
        self.webdriver.get(url)
        self.webdriver.execute_script("document.title = '%s'" %
                                      threading.current_thread().name.replace("'", '"'))

    def run_test(self, test):
        """Run a single test.

        This method is independent of the test type, and calls
        do_test to implement the type-sepcific testing functionality.
        """
        # Lock to prevent races between timeouts and other results
        # This might not be strictly necessary if we need to deal
        # with the result changing post-hoc anyway (e.g. due to detecting
        # a crash after we get the data back from webdriver)
        result = None
        result_flag = threading.Event()
        result_lock = threading.Lock()

        timeout = test.timeout * self.timeout_multiplier

        def timeout_func():
            with result_lock:
                if not result_flag.is_set():
                    result_flag.set()
                    result = (test.result_cls("EXTERNAL-TIMEOUT", None), [])
                    self.runner.send_message("test_ended", test, result)

        self.timer = threading.Timer(timeout + 10, timeout_func)
        self.timer.start()

        try:
            self.webdriver.set_script_timeout((timeout + 5) * 1000)
        except exceptions.ErrorInResponseException:
            self.logger.error("Lost webdriver connection")
            self.runner.send_message("restart_test", test)
            return Stop

        try:
            result = self.convert_result(test, self.do_test(test, timeout))
        except exceptions.TimeoutException:
            with result_lock:
                if not result_flag.is_set():
                    result_flag.set()
                    result = (test.result_cls("EXTERNAL-TIMEOUT", None), [])
            # Clean up any unclosed windows
            # This doesn't account for the possibility the browser window
            # is totally hung. That seems less likely since we are still
            # getting data from marionette, but it might be just as well
            # to do a full restart in this case
            # XXX - this doesn't work at the moment because window_handles
            # only returns OS-level windows (see bug 907197)
            # while True:
            #     handles = self.marionette.window_handles
            #     self.marionette.switch_to_window(handles[-1])
            #     if len(handles) > 1:
            #         self.marionette.close()
            #     else:
            #         break
            # Now need to check if the browser is still responsive and restart it if not

        # TODO: try to detect crash here
        except (socket.timeout, exceptions.ErrorInResponseException):
            # This can happen on a crash
            # Also, should check after the test if the firefox process is still running
            # and otherwise ignore any other result and set it to crash
            with result_lock:
                if not result_flag.is_set():
                    result_flag.set()
                    result = (test.result_cls("CRASH", None), [])
        finally:
            self.timer.cancel()

        with result_lock:
            if result:
                self.runner.send_message("test_ended", test, result)


class IETestharnessExecutor(IETestExecutor):
    convert_result = testharness_result_converter

    def __init__(self, *args, **kwargs):
        IETestExecutor.__init__(self, *args, **kwargs)
        self.script = open(os.path.join(here, "testharness_ie11.js")).read()

    def do_test(self, test, timeout):
        result = self.webdriver.execute_async_script(
            self.script % {"abs_url": urlparse.urljoin(self.http_server_url, test.url),
                           "url": test.url,
                           "window_id": self.window_id,
                           "timeout_multiplier": self.timeout_multiplier,
                           "timeout": timeout * 1000})
        return result

class IEReftestExecutor(IETestExecutor):
    convert_result = reftest_result_converter

    def __init__(self, *args, **kwargs):
        IETestExecutor.__init__(self, *args, **kwargs)
        with open(os.path.join(here, "reftest.js")) as f:
            self.script = f.read()
        with open(os.path.join(here, "reftest-wait.js")) as f:
            self.wait_script = f.read()
        self.ref_hashes = {}
        self.ref_urls_by_hash = defaultdict(set)

    def do_test(self, test, timeout):
        test_url, ref_type, ref_url = test.url, test.ref_type, test.ref_url
        hashes = {"test": None,
                  "ref": self.ref_hashes.get(ref_url)}
        images = {"test": None,
                  "ref": None}

        test_uuid = uuid.uuid1()
        self.webdriver.execute_script(self.script)
        self.webdriver.switch_to_window(self.webdriver.window_handles[-1])

        for url_type, url in [("test", test_url), ("ref", ref_url)]:
            if hashes[url_type] is None:
                # Would like to do this in a new tab each time, but that isn't
                full_url = urlparse.urljoin(self.http_server_url, url)
                try:
                    # self.logger.debug("get: %i" % full_url)
                    self.webdriver.get(full_url)
                except:
                    # self.logger.debug("Unexpected error: %i", sys.exc_info()[0])
                    return {"status": "ERROR",
                            "message": "Failed to load url %s" % (full_url,)}
                if url_type == "test":
                    pass
                self.wait()
                #images[url_type] = str(test_uuid) + url_type + ".png"
                images[url_type] = self.webdriver.get_screenshot_as_base64()

        image_comparer = ImageComparer()
        are_equal = image_comparer.compare(images["test"], images["ref"])
        if ref_type == "==":
            passed = are_equal
        elif ref_type == "!=":
            passed = not are_equal
        else:
            raise ValueError

        #for image_type, image_path in images.iteritems():
        #    os.remove(".\\" + image_path)

        return {"status": "PASS" if passed else "FAIL",
                "message": None}

    def wait(self):
        # self.webdriver.execute_async_script(self.wait_script)
        self.logger.debug("### WAIT ###")
        # time.sleep(3000)

    def teardown(self):
        count = 0
        for hash_val, urls in self.ref_urls_by_hash.iteritems():
            if len(urls) > 1:
                self.logger.info("The following %i reference urls appear to be equivalent:\n %s" %
                                 (len(urls), "\n  ".join(urls)))
                count += len(urls) - 1
        IETestExecutor.teardown(self)
