#!/usr/bin/env python
#Changelog
# 0.1 Initial Version
# 0.1.1 Correct number of reviews
# 0.1.2 Fixed SSL
# 0.2 Removed [buy] posts
# 0.2.1 Fixed selfposts being ignored in listings
# 0.2.2 Fixed more than 25 listings
# 0.2.3 Fixed listings for usernames starting with a hyphen
# 0.2.4 Bot ignores [rvw] listings
# 0.2.5 Fixed Unicode characters not being matched by regex
# 0.3 message overhaul
# 0.3.1 fixed 
# 0.3.3 Changed timezone
# 0.3.4 Adjusted for Sold pm
# 0.3.5 Fixed spamming
# 0.4 OAuth and proper caching
# 0.4.1 Adjusted tokenpath to work with cron
# 0.4.2 Regex doesn't match [Bra] now
# 0.5.0 Major rewrite by /u/irrational_function
# 0.5.1 Initial version for switch
# 0.5.2 Switch to cloudsearch syntax to handle usernames with hyphens
# 0.5.3 Fix line break in PM

bot_version = '0.5.3'
bot_author = 'irrational_function'

import time
import re
import urllib
import sqlite3
import praw
import argparse
import yaml
import logging
import logging.config

def iter_count(iter):
    return sum(1 for _ in iter)

class SexbotDB:

    def __init__(self, filename, max_tries=10):
        self.conn = sqlite3.connect(filename)
        self.max_tries = max_tries
        cur = self.cursor()
        cur.execute('pragma user_version')
        user_version = cur.fetchone()[0]
        if user_version == 0:
            self.create_db(cur)
        elif user_version != 1:
            raise Exception('invalid db user_version')

    COMMENT = 1
    MAIL = 2

    def create_db(self, cur):
        cur.execute('create table access_info (access_token text, refresh_time integer)')
        cur.execute('create table seen_things (id text primary key not null, seen_time integer not null)')
        cur.execute('create table work_queue (id text not null, action integer not null, '+
                    'when_time real not null, tries integer not null)')
        cur.execute('create unique index work_queue_items on work_queue (id, action)')
        cur.execute('create index work_queue_when on work_queue (when_time)')
        cur.execute('create index seen_things_time on seen_things (seen_time)')
        cur.execute('insert into access_info values (NULL, NULL)')
        cur.execute('pragma user_version=1')
        self.commit()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def cursor(self):
        return self.conn.cursor()

    def is_thing_seen(self, thing):
        cur = self.cursor()
        cur.execute('select * from seen_things where id = ?', (thing.fullname,))
        return cur.fetchone() is not None

    def mark_thing_seen(self, thing):
        cur = self.cursor()
        cur.execute('insert into seen_things values (?, ?)', (thing.fullname, int(thing.created_utc)))

    def add_action(self, post, action):
        if action == self.MAIL:
            when = float(post.created_utc + 6*60*60)
        else:
            when = time.time()
        cur = self.cursor()
        cur.execute('insert into work_queue values (?, ?, ?, 0)', (post.id, action, when))

    def get_pending_actions(self):
        now = time.time()
        cur = self.cursor()
        cur.execute('select * from work_queue where when_time <= ? order by when_time', (now,))
        return cur.fetchall()

    def get_wait_time(self, max_wait):
        now = time.time()
        cur = self.cursor()
        cur.execute('select when_time from work_queue order by when_time limit 1')
        item = cur.fetchone()
        if item is not None:
            next_time = item[0]
            if next_time <= now:
                return 0.0
            else:
                return min(max_wait, next_time-now)
        return max_wait

    def remove_action(self, item):
        cur = self.cursor()
        cur.execute('delete from work_queue where id=? and action=?', item[0:2])

    def backoff_action(self, item):
        (post_id, action, when, tries) = item
        if tries + 1 < self.max_tries:
            tries += 1
            delay = 60 * min(60, 2**tries)
            when = time.time() + delay
            cur = self.cursor()
            cur.execute('update work_queue set when_time=?, tries=? where id=? and action=?',
                        (when, tries, post_id, action))
            return False
        else:
            self.remove_action(item)
            return True

    def get_creds(self):
        cur = self.cursor()
        cur.execute('select * from access_info')
        return cur.fetchone()

    def update_creds(self, token, timestamp):
        cur = self.cursor()
        cur.execute('update access_info set access_token=?, refresh_time=?', (token, timestamp))
        self.commit()

    def clear_creds(self):
        self.update_creds(None, None)


def get_registered_days(user):
    t = user.created_utc
    now = time.time()
    return int((now - t) / (24 * 60 * 60))

def utf8_url_quote_plus(s):
    return urllib.quote_plus(s.encode('utf-8'))

def create_mail_link(anchor_text, recip, subject=None, message=None):
    link = ['[', anchor_text, '](/message/compose/?to=', utf8_url_quote_plus(recip)]
    if subject is not None:
        link += ['&subject=', utf8_url_quote_plus(subject)]
    if message is not None:
        link += ['&message=', utf8_url_quote_plus(message)]
    link.append(')')
    return ''.join(link)

class SexbotSubredditUtils:

    def __init__(self, subreddit):
        self.sr = subreddit

    def get_search_count_and_link(self, query):
        s = self.sr.search(query, syntax='cloudsearch', limit=None)
        count = iter_count(s)
        ret = '**' + str(count) + '** [view](/r/' + self.sr.display_name + '/search?q='
        ret += utf8_url_quote_plus(query) + '&syntax=cloudsearch&sort=new&restrict_sr=on)'
        return ret

    def get_flair(self, user):
        flair = self.sr.get_flair(user)
        css = flair['flair_css_class']
        if css == 'verified' or css == 'verifiedmod':
            return 'Verified Seller'
        elif css == 'trustedseller' or css == 'trustedmod':
            return 'Trusted Seller'
        else:
            return None

    def create_comment(self, post):
        user = post.author
        if user is None:
            return None
        flair = self.get_flair(user)
        if flair is None:
            return None
        days = str(get_registered_days(user))
        gentime = time.strftime('%T UTC %F', time.gmtime())
        karma = str(user.link_karma + user.comment_karma)
        listings = self.get_search_count_and_link("(field author '" + user.name + "')")
        rvw_query = "(and (field flair 'review') (field title '" + user.name + "'))"
        reviews = self.get_search_count_and_link(rvw_query)
        msg = ['###SexSells Stats for /u/' + user.name]
        msg.append('* Verification: **' + flair + '** [learn more](/r/Sexsells/w/verification)')
        msg.append('* Account Age: **' + days + '** Days | Karma: **' + karma + '**')
        msg.append('* No. of Listings: ' + listings + ' | No. of Reviews: ' + reviews)
        msg.append('')
        msg.append('---')
        msg.append('')
        msg.append('[Wiki](/r/Sexsells/w/) | [FAQ](/r/Sexsells/w/faq) | [Bot Info](/r/Sexsells/w/bot) | ')
        msg.append(create_mail_link('Report a Bug', bot_author, subject='SexStatsBot Bug',
                                    message='The post with a bug is: ' + post.short_link) + ' | ')
        msg.append(create_mail_link('Modmail', '/r/Sexsells'))
        msg.append('')
        msg.append('---')
        msg.append('^(Version ' + bot_version + '. Generated at: ' + gentime + ')')
        msg_str = '\n'.join(msg)
        return msg_str

    physical_item_body = """
If your item sells, please remember to mark it as "Sold" by changing your listing's link flair. [View instructions here](http://fat.gfycat.com/JoyousIdenticalFairybluebird.gif) ('refresh' the page to view new flair).

If this message was sent in error, check that your listing is correctly marked as a 'Physical' or 'Digital' item. You can use the instructions above to change it.

If the "Sold" flair won't apply because you are selling more than one of an item (like pussy pops and vials of grool), ignore this message.

Have a question? [Message the moderators](/message/compose?to=%2Fr%2FSexsells).

*This action was performed by a bot. Please do not respond to this message.*"""

    def create_mail(self, post):
        if post.author is None or post.link_flair_css_class != 'physical':
            return None
        subject = 'Physical item reminder from /r/Sexsells'
        msg = ['**Your listing: <' + post.permalink + '>**']
        msg.append(self.physical_item_body)
        msg.append(create_mail_link('Report a Bot Bug', bot_author, subject='SexStatsBot PM Bug',
                                    message='The mail with the bug was sent regarding: '+post.short_link))
        msg.append('ID:'+ post.id + ':')
        msg_str = '\n'.join(msg)
        return {'subject': subject, 'message': msg_str}


class Sexbot:

    def __init__(self, config, logger):
        self.user_agent = 'Linux:Sexsells stats script:v' + bot_version + ' (by /u/' + bot_author + ')'
        self.ignore_re = re.compile('\[(meta|rvw|buy)\]', re.IGNORECASE)
        self.postid_re = re.compile('ID:([A-Za-z0-9]+):')
        self.oauth_scope = set([u'read', u'identity', u'privatemessages', u'submit', u'modflair'])
        self.log = logger
        self.refresh_token = config['oauth_refresh_token']
        self.cutin_time = int(config['cutin_time'])
        self.db = SexbotDB(config['dbpath'])
        self.reddit = praw.Reddit(user_agent=self.user_agent)
        self.reddit.set_oauth_app_info(config['oauth_client_id'], config['oauth_client_secret'], config['oauth_redirect_uri'])
        self.subreddit = self.reddit.get_subreddit(config['subreddit'])
        self.utils = SexbotSubredditUtils(self.subreddit)

    def ensure_auth(self):
        cred = self.db.get_creds()
        if cred[1] is not None and time.time() < cred[1] + 1800:
            if not self.reddit.is_oauth_session():
                self.reddit.set_access_credentials(self.oauth_scope, cred[0], self.refresh_token)
            return
        timestamp = int(time.time())
        access_info = self.reddit.refresh_access_information(self.refresh_token)
        self.db.update_creds(access_info['access_token'], timestamp)

    def handle_new_items(self, items, handler):
        ordered = []
        for item in items:
            if item.created_utc < self.cutin_time or self.db.is_thing_seen(item):
                break
            ordered.append(item)
        ordered.reverse()
        for item in ordered:
            self.db.mark_thing_seen(item)
            handler(item)
            self.db.commit()

    def handle_new_posts(self):
        def queue_post_work(post):
            author_name = ('[deleted]' if post.author is None else post.author.name)
            if post.author is None or self.ignore_re.search(post.title):
                self.log.info('Skipping post %s by %s; %s', post.id, author_name, post.title)
                return
            self.log.info('Queuing post %s by %s; %s', post.id, author_name, post.title)
            self.db.add_action(post, self.db.COMMENT)
            self.db.add_action(post, self.db.MAIL)
        posts = self.subreddit.get_new(limit=None)
        self.handle_new_items(posts, queue_post_work)

    def handle_new_sents(self):
        def flag_mail_done(message):
            match = self.postid_re.search(message.body)
            if match:
                item = (match.group(1), self.db.MAIL)
                self.db.remove_action(item)
                self.log.info('Read sent-message %s, sent for post %s', message.id, item[0])
            else:
                self.log.warning('Found no post id in sent-message %s', message.id)
        sent_mail = self.reddit.get_sent(limit=None)
        self.handle_new_items(sent_mail, flag_mail_done)

    def handle_new_comments(self):
        def flag_post_done(comment):
            item = (comment.submission.id, self.db.COMMENT)
            self.db.remove_action(item)
            self.log.info('Read self-comment %s on post %s', comment.id, item[0])
        comments = self.reddit.user.get_comments(limit=None)
        self.handle_new_items(comments, flag_post_done)

    def do_comment(self, post):
        comment_text = self.utils.create_comment(post)
        if comment_text is not None:
            self.log.info('Commenting on post %s', post.id)
            comment = post.add_comment(comment_text)
            self.log.info('Added comment %s on post %s', comment.id, post.id)
            return True
        else:
            self.log.info('No comment on post %s', post.id)
            return False

    def do_mail(self, post):
        mail = self.utils.create_mail(post)
        if mail is not None:
            self.log.info('Sending mail for post %s to %s', post.id, post.author.name)
            self.reddit.send_message(post.author, raise_captcha_exception=True, **mail)
            self.log.info('Sent mail for post %s', post.id)
            return True
        else:
            self.log.info('Not sending mail for post %s', post.id)
            return False

    def handle_work_item(self, item):
        final = self.db.backoff_action(item)
        self.db.commit()
        (postid, action) = item[0:2]
        final_str = ('; final attempt' if final else '')
        self.log.info('Handling queued post %s for action %s, try %s%s', postid, action, item[3]+1, final_str)
        try:
            post = self.reddit.get_submission(submission_id=postid)
        except praw.errors.NotFound as e:
            post = None
            self.log.warning('Queued post %s was not found', postid)
        ret = False
        if post is not None:
            if post.banned_by is not None:
                self.log.info('Queued post %s was banned', postid)
            elif post.author is None:
                self.log.info('Queued post %s was deleted', postid)
            elif action == self.db.COMMENT:
                ret = self.do_comment(post)
            elif action == self.db.MAIL:
                ret = self.do_mail(post)
        self.db.remove_action(item)
        self.db.commit()
        return ret

    def handle_work_queue(self):
        ret = False
        for item in self.db.get_pending_actions():
            ret |= self.handle_work_item(item)
        return ret

    def handle_iteration(self, self_update):
        try:
            self.ensure_auth()
            self.handle_new_posts()
            if self_update:
                self.handle_new_comments()
                self.handle_new_sents()
            return self.handle_work_queue()
        except Exception as e:
            self.db.rollback()
            raise

    def loop(self, delay=60):
        need_update = True
        while True:
            try:
                do_update = need_update
                need_update = True
                next_delay = delay
                need_update = self.handle_iteration(do_update)
                next_delay = self.db.get_wait_time(delay)
            except praw.errors.RateLimitExceeded as e:
                next_delay = max(e.sleep_time, delay)
                self.log.warning('Rate limit exceeded, sleep_time = %s', e.sleep_time)
            except praw.errors.OAuthInvalidToken as e:
                self.db.clear_creds()
                next_delay = 1
                self.log.warning('Invalid OAuth token, refreshing')
            except Exception as e:
                self.log.exception(e)
            time.sleep(next_delay)

def get_settings(filename='sexbot.conf'):
    with open(filename) as f:
        return yaml.safe_load(f)

def parse_args(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=None)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('-v', '--verbose', dest='logger', action='store_const',
                        const='sexbot.verbose', default=None)
    return parser.parse_args(args)

def make_bot(config_name=None, logger_name=None):
    if config_name is None: config_name = 'default'
    if logger_name is None: logger_name = 'sexbot.default'
    settings = get_settings()
    logging.config.dictConfig(settings['logging'])
    config = settings['configs'][config_name]
    logger = logging.getLogger(logger_name)
    return Sexbot(config, logger)

def main(args):
    bot = make_bot(args.config, args.logger)
    if args.once:
        bot.handle_iteration()
    else:
        bot.loop()

if __name__ == '__main__':
    main(parse_args())
