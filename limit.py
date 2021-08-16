#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - limit.py
# 8/15/21 18:23
#

__author__ = "Benny <benny.think@gmail.com>"

import hashlib
import logging
import math
import os
import sqlite3
import tempfile
import time

import redis
import requests

# QUOTA = 10 * 1024 * 1024 * 1024  # 10G
QUOTA = 5 * 1024 * 1024  # 10G
EX = 24 * 3600
MULTIPLY = 5  # VIP1 is 5*10-50G, VIP2 is 100G
USD2CNY = 6  # $5 --> ¥30


class Redis:
    def __init__(self):
        super(Redis, self).__init__()
        self.r = redis.StrictRedis(host=os.getenv("REDIS") or "redis", db=4, decode_responses=True)

    def __del__(self):
        self.r.close()


class SQLite:
    def __init__(self):
        super(SQLite, self).__init__()
        self.con = sqlite3.connect("vip.sqlite", check_same_thread=False)
        self.cur = self.con.cursor()
        SQL = """
            create table if not exists VIP
            (
                user_id    integer 
                    constraint VIP
                        primary key,
                username varchar(100),
                payment_amount    integer,
                payment_id varchar(100),
                level     integer default 1,
                quota int default %s
            );
        """ % QUOTA
        self.cur.execute(SQL)
        # SQL = """create unique index VIP_payment_id_uindex on VIP (payment_id);"""
        # self.cur.execute(SQL)

    def __del__(self):
        self.con.close()


def get_username(chat_id):
    from ytdl import create_app
    with tempfile.NamedTemporaryFile() as tmp:
        app = create_app(tmp.name, 1)
        app.start()
        data = app.get_chat(chat_id).first_name
        app.stop()

    return data


class VIP(Redis, SQLite):

    def check_vip(self, user_id: "int") -> "tuple":
        self.cur.execute("SELECT * FROM VIP WHERE user_id=?", (user_id,))
        data = self.cur.fetchone()
        return data

    def add_vip(self, user_data: "dict") -> ("bool", "str"):
        sql = "INSERT INTO VIP VALUES (?,?,?,?,?,?);"
        # first select
        self.cur.execute("SELECT * FROM VIP WHERE payment_id=?", (user_data["payment_id"],))
        is_exist = self.cur.fetchone()
        if is_exist:
            return "Failed. {} is being used by user {}".format(user_data["payment_id"], is_exist[0])
        self.cur.execute(sql, list(user_data.values()))
        self.con.commit()
        # also remove redis cache
        self.r.delete(user_data["user_id"])
        return "Success! You are VIP{} now!".format(user_data["level"])

    def remove_vip(self, user_id: "int"):
        raise NotImplementedError()

    def get_user_quota(self, user_id: "int") -> int:
        # even VIP have certain quota
        q = self.check_vip(user_id)
        return q[-1] if q else QUOTA

    def check_remaining_quota(self, user_id: "int"):
        user_quota = self.get_user_quota(user_id)
        ttl = self.r.ttl(user_id)
        q = int(self.r.get(user_id)) if self.r.exists(user_id) else user_quota
        if q <= 0:
            q = 0
        return q, user_quota, ttl

    def use_quota(self, user_id: "int", traffic: "int"):
        user_quota = self.get_user_quota(user_id)
        if self.r.exists(user_id):
            self.r.decr(user_id, traffic)
        else:
            self.r.set(user_id, user_quota - traffic, ex=EX)


class BuyMeACoffee:
    def __init__(self):
        self._token = os.getenv("COFFEE_TOKEN")
        self._url = "https://developers.buymeacoffee.com/api/v1/supporters"
        self._data = []

    def _get_data(self, url):
        d = requests.get(url, headers={"Authorization": f"Bearer {self._token}"}).json()
        self._data.extend(d["data"])
        next_page = d["next_page_url"]
        if next_page:
            self._get_data(next_page)

    def _get_bmac_status(self, email: "str") -> "dict":
        self._get_data(self._url)
        for user in self._data:
            if user["payer_email"] == email or user["support_email"] == email:
                return user
        return {}

    def get_user_payment(self, email: "str") -> ("int", "float", "str"):
        order = self._get_bmac_status(email)
        amount = float(order.get("support_coffee_price", 0))
        level = math.floor(amount / MULTIPLY)
        return level, amount, email


class Afdian:
    def __init__(self):
        self._token = os.getenv("AFD_TOKEN")
        self._user_id = os.getenv("AFD_USER_ID")
        self._url = "https://afdian.net/api/open/query-order"

    def _generate_signature(self):
        data = {
            "user_id": self._user_id,
            "params": "{\"x\":0}",
            "ts": int(time.time()),
        }
        sign_text = "{token}params{params}ts{ts}user_id{user_id}".format(
            token=self._token, params=data['params'], ts=data["ts"], user_id=data["user_id"]
        )

        md5 = hashlib.md5(sign_text.encode("u8"))
        md5 = md5.hexdigest()
        data["sign"] = md5

        return data

    def _get_afdian_status(self, trade_no: "str") -> "dict":
        req_data = self._generate_signature()
        data = requests.post(self._url, json=req_data).json()
        # latest 50
        for order in data["data"]["list"]:
            if order["out_trade_no"] == trade_no:
                return order

        return {}

    def get_user_payment(self, trade_no: "str") -> ("int", "float", "str"):
        order = self._get_afdian_status(trade_no)
        amount = float(order.get("show_amount", 0))
        level = math.floor(amount / (MULTIPLY * USD2CNY))
        return level, amount, trade_no


def verify_payment(user_id, unique) -> "str":
    logging.info("Verifying payment for %s - %s", user_id, unique)
    if "@" in unique:
        pay = BuyMeACoffee()
    else:
        pay = Afdian()

    level, amount, pay_id = pay.get_user_payment(unique)
    if amount == 0:
        return f"You pay amount is {amount}. Did you input wrong order ID or email? " \
               f"Talk to @BennyThink if you need any assistant."
    if not level:
        return f"You pay amount {amount} is below minimum ${MULTIPLY}. " \
               f"Talk to @BennyThink if you need any assistant."
    else:
        vip = VIP()
        ud = {
            "user_id": user_id,
            "username": get_username(user_id),
            "payment_amount": amount,
            "payment_id": pay_id,
            "level": level,
            "quota": QUOTA * level * MULTIPLY
        }

        message = vip.add_vip(ud)
        return message


if __name__ == '__main__':
    pass
