﻿import hmac
import os
import struct
import time

import binascii
from typing import Union

from peewee import *
import config
from lib.utils import get_today_start_timestamp
from model._post import PostModel
from model.log_manage import ManageLog
from model.redis import redis, RK_USER_ACTCODE_BY_USER_ID, RK_USER_RESET_KEY_BY_USER_ID, \
    RK_USER_LAST_REQUEST_ACTCODE_BY_USER_ID, RK_USER_LAST_REQUEST_RESET_KEY_BY_USER_ID
from slim.base.user import BaseUser
from slim.utils import StateObject, to_hex, to_bin
from model import BaseModel, MyTimestampField, CITextField, db


class USER_GROUP(StateObject):
    BAN = 10
    INACTIVE = 40
    NORMAL = 50
    SUPERUSER = 90
    ADMIN = 100

    GROUP_TO_ROLE = {
        BAN: 'ban',
        INACTIVE: 'inactive_user',
        NORMAL: 'user',
        SUPERUSER: 'superuser',
        ADMIN: 'admin'
    }
    txt = {BAN: '封禁', INACTIVE: '未激活', NORMAL: '会员', SUPERUSER: '超级用户', ADMIN: '管理'}


def get_user_count_seq():
    return db.execute_sql("select nextval('user_count_seq')").fetchone()[0]


class User(PostModel, BaseUser):
    email = TextField(index=True, unique=True)
    nickname = CITextField(index=True, unique=True)  # CITextField
    password = BlobField()
    salt = BlobField()  # auto
    biology = TextField(null=True)  # 简介
    avatar = TextField(null=True)
    type = IntegerField(default=0)  # 账户类型，0默认，1组织
    url = TextField(null=True)  # 个人主页
    location = TextField(null=True)  # 所在地

    # level = IntegerField(index=True)  # 用户级别
    group = IntegerField(index=True)  # 用户权限组

    key = BlobField(index=True, null=True)
    key_time = MyTimestampField()  # 最后登录时间
    access_time = MyTimestampField(null=True, default=None)  # 最后访问时间，以misc为准吧
    last_check_in_time = MyTimestampField(null=True, default=None)  # 上次签到时间
    check_in_his = IntegerField(default=0)  # 连续签到天数

    phone = TextField(null=True, default=None)  # 大陆地区
    number = IntegerField(default=get_user_count_seq)  # 序号，第N个用户 sequence='user_count_seq'
    credit = IntegerField(default=0)  # 积分，会消费
    exp = IntegerField(default=0)  # 经验值，不会消失
    reputation = IntegerField(default=0)  # 声望

    reset_key = BlobField(index=True, null=True, default=None)  # 重置密码所用key

    class Meta:
        db_table = 'user'

    #object_type = OBJECT_TYPES.USER

    @classmethod
    def find_by_nicknames(cls, names):
        it = iter(names)
        try:
            condition = cls.nickname == next(it)
        except StopIteration:
            return []
        for i in it:
            condition |= cls.nickname == i
        return cls.select().where(condition)

    @property
    def roles(self):
        ret = [None]
        if self.group >= USER_GROUP.ADMIN:
            ret.append('admin')
        if self.group >= USER_GROUP.SUPERUSER:
            ret.append('superuser')
        if self.group >= USER_GROUP.NORMAL:
            ret.append('user')
        if self.group >= USER_GROUP.INACTIVE:
            ret.append('inactive_user')
        return ret

    @classmethod
    def gen_id(cls):
        return config.POST_ID_GENERATOR()

    @classmethod
    def gen_password_and_salt(cls, password_text):
        salt = os.urandom(16)
        m = hmac.new(salt, digestmod=config.PASSWORD_HASH_FUNC)
        m.update(password_text.encode('utf-8'))
        return {'password': m.digest(), 'salt': salt}

    @classmethod
    def gen_key(cls):
        key = os.urandom(16)
        key_time = int(time.time())
        return {'key': key, 'key_time': key_time}

    def refresh_key(self):
        count = 0
        while count < 10:
            with db.atomic():
                try:
                    k = self.gen_key()
                    self.key = k['key']
                    self.key_time = k['key_time']
                    self.save()
                    return k
                except DatabaseError:
                    count += 1
                    db.rollback()
        raise ValueError("generate key failed")

    @classmethod
    def get_by_key(cls, key):
        try:
            return cls.get(cls.key == key)
        except DoesNotExist:
            return None

    async def can_request_actcode(self):
        """
        是否能申请帐户激活码（用于发送激活邮件）
        :return:
        """
        if self.group != USER_GROUP.INACTIVE:
            return
        val = await redis.get(RK_USER_LAST_REQUEST_ACTCODE_BY_USER_ID % self.id)
        if val is None:
            return True
        if time.time() - int(val) > config.USER_ACTIVATION_REQUEST_INTERVAL:
            return True

    async def gen_activation_code(self) -> bytes:
        """
        生成一个账户激活码
        :return:
        """
        t = int(time.time())
        code = os.urandom(8)
        pipe = redis.pipeline()
        pipe.set(RK_USER_LAST_REQUEST_ACTCODE_BY_USER_ID % self.id, t)
        pipe.set(RK_USER_ACTCODE_BY_USER_ID % self.id, code, expire=config.USER_ACTCODE_EXPIRE)
        await pipe.execute()
        return code

    @classmethod
    async def check_actcode(cls, uid, code):
        """
        检查账户激活码是否可用，若可用，激活账户
        :param uid:
        :param code:
        :return:
        """
        if not code: return
        if isinstance(uid, str): uid = to_bin(uid)
        if isinstance(code, str): code = to_bin(code)

        if len(code) == 8:
            rkey = RK_USER_ACTCODE_BY_USER_ID % uid
            if await redis.get(rkey) == code:
                try:
                    u = cls.get(cls.id == uid, cls.group == USER_GROUP.INACTIVE)
                    u.group = USER_GROUP.NORMAL
                    u.save()
                    await redis.delete(rkey)
                    return u
                except cls.DoesNotExist:
                    pass

    async def can_request_reset_password(self):
        """
        是否能申请重置密码
        :return:
        """
        val = await redis.get(RK_USER_LAST_REQUEST_RESET_KEY_BY_USER_ID % self.id)
        if val is None:
            return True
        if time.time() - int(val) > config.USER_RESET_PASSWORD_CODE_EXPIRE:
            return True

    def gen_reset_key(self) -> bytes:
        """
        生成一个重置密码key
        :return:
        """
        # len == 16 + 8 == 24
        t = int(time.time())
        code = os.urandom(16) + t.to_bytes(8, 'little')
        redis.set(RK_USER_LAST_REQUEST_RESET_KEY_BY_USER_ID % self.id, t, expire=config.USER_RESET_PASSWORD_REQUST_INTERVAL)
        redis.set(RK_USER_RESET_KEY_BY_USER_ID % self.id, code, expire=config.USER_RESET_PASSWORD_CODE_EXPIRE)
        return code

    @classmethod
    async def check_reset_key(cls, uid, code) -> Union['User', None]:
        """
        检查uid与code这一组密码重置密钥是否有效
        :param uid:
        :param code:
        :return:
        """
        if not code: return
        if isinstance(uid, str): uid = to_bin(uid)
        if isinstance(code, str): code = to_bin(code)

        if len(code) == 24:
            rkey = RK_USER_RESET_KEY_BY_USER_ID % uid
            if await redis.get(rkey) == code:
                try:
                    u = cls.get(cls.id == uid)
                    await redis.delete(rkey)
                    return u
                except cls.DoesNotExist:
                    pass

    def set_password(self, new_password):
        info = self.gen_password_and_salt(new_password)
        self.salt = info['salt']
        self.password = info['password']
        self.save()

    def update_access_time(self):
        self.access_time = int(time.time())
        self.save()
        return self.access_time

    def check_in(self):
        self.last_check_in_time = self.last_check_in_time or 0
        old_time = self.last_check_in_time
        last_midnight = get_today_start_timestamp()

        # 今日未签到
        if self.last_check_in_time < last_midnight:
            self.last_check_in_time = int(time.time())
            # 三天内有签到，连击
            if old_time > last_midnight - config.USER_CHECKIN_COUNTER_INTERVAL:
                self.check_in_his += 1
            else:
                self.check_in_his = 1
            self.save()

            # 签到加分
            credit = self.credit
            exp = self.exp
            self.credit += 5
            self.exp += 5
            self.save()
            ManageLog.add_by_credit_changed_sys(self, note='每日签到', value=[credit, self.credit])
            ManageLog.add_by_exp_changed_sys(self, note='每日签到', value=[exp, self.exp])

            return {
                'credit': 5,
                'exp': 5,
                'time': self.last_check_in_time,
                'check_in_his': self.check_in_his
            }

    def daily_access_reward(self):
        self.access_time = self.access_time or 0
        old_time = self.access_time
        self.update_access_time()

        if old_time < get_today_start_timestamp():
            exp = self.exp
            self.exp += 5
            self.save()
            ManageLog.add_by_exp_changed_sys(self, note='每日登录', value=[exp, self.exp])
            return 5

    @classmethod
    def auth(cls, email, password_text):
        try:
            u = cls.get(cls.email == email)
        except DoesNotExist:
            return False

        m = hmac.new(u.salt.tobytes(), digestmod=config.PASSWORD_HASH_FUNC)
        m.update(password_text.encode('utf-8'))

        if u.password.tobytes() == m.digest():
            return u

    def __repr__(self):
        return '<User id:%x nickname:%r>' % (int.from_bytes(self.id.tobytes(), 'big'), self.nickname)
