"""POP3Proxy and SMTPProxy Web Interface

Classes:
    ProxyUserInterface - Interface class for pop3proxy and smtpproxy

Abstract:

This module implements a browser based Spambayes user interface for the
POP3 proxy and SMTP proxy.  Users may use it to interface with the
proxies.

The following functions are currently included:
[From the base class UserInterface]
  onClassify - classify a given message
  onWordquery - query a word from the database
  onTrain - train a message or mbox
  onSave - save the database and possibly shutdown
[Here]
  onHome - a home page with various options
  onUpload - upload a message for later training (used by proxytee.py)
  onReview - show messages in corpii
  onView - view a message from one of the corpii
  onShowclues - show clues for a message

To do:

Web training interface:

 o Review already-trained messages, and purge them.
 o Put in a link to view a message (plain text, html, multipart...?)
   Include a Reply link that launches the registered email client, eg.
   mailto:tim@fourstonesExpressions.com?subject=Re:%20pop3proxy&body=Hi%21%0D
 o [Francois Granger] Show the raw spambrob number close to the buttons
   (this would mean using the extra X-Hammie header by default).
 o Add Today and Refresh buttons on the Review page.

User interface improvements:

 o Can it cleanly dynamically update its status display while having a POP3
   conversation?  Hammering reload sucks.

 o Suggestions?
"""

# This module is part of the spambayes project, which is Copyright 2002
# The Python Software Foundation and is covered by the Python Software
# Foundation license.

# This module was once part of pop3proxy.py; if you are looking through
# the history of the file, you may need to go back there.

__author__ = "Richie Hindle <richie@entrian.com>"
__credits__ = "Tim Peters, Neale Pickett, Tim Stone, all the Spambayes folk."

try:
    True, False
except NameError:
    # Maintain compatibility with Python 2.2
    True, False = 1, 0

import re
import time
import bisect

import tokenizer
import UserInterface
from Options import options

global state

# This control dictionary maps http request parameters and template fields
# to ConfigParser sections and options.  The key matches both the input
# field that corresponds to a section/option, and also the HTML template
# variable that is used to display the value of that section/option.
parm_ini_map = \
   {'hamcutoff':    ('Categorization',  'ham_cutoff'),
    'spamcutoff':   ('Categorization',  'spam_cutoff'),
    'dbname':       ('pop3proxy',       'persistent_storage_file'),
    'p3servers':    ('pop3proxy',       'servers'),
    'p3ports':      ('pop3proxy',       'ports'),
    'p3notateto':   ('pop3proxy',       'notate_to'),
    'p3notatesub':  ('pop3proxy',       'notate_subject'),
    'p3cachemsg':   ('pop3proxy',       'cache_messages'),
    'p3addid':      ('pop3proxy',       'add_mailid_to'),
    'p3stripid':    ('pop3proxy',       'strip_incoming_mailids'),
    'p3prob':       ('pop3proxy',       'include_prob'),
    'p3thermostat': ('pop3proxy',       'include_thermostat'),
    'p3evidence':   ('pop3proxy',       'include_evidence'),
    'smtpservers':  ('smtpproxy',       'servers'),
    'smtpports':    ('smtpproxy',       'ports'),
    'smtpham':      ('smtpproxy',       'ham_address'),
    'smtpspam':     ('smtpproxy',       'spam_address'),
   }

display = ('POP3 Proxy Options', 'p3servers', 'p3ports', 'p3cachemsg',
           'Header Options', 'p3notateto', 'p3notatesub', 
           'p3prob', 'p3thermostat', 'p3evidence', 
           'p3addid', 'p3stripid',
           'SMTP Proxy Options', 'smtpservers', 'smtpports', 'smtpham',
           'smtpspam',
           'Statistics Options', 'dbname', 'hamcutoff', 'spamcutoff')


class ProxyUserInterface(UserInterface.UserInterface):
    """Serves the HTML user interface for the proxies."""

    def __init__(self, proxy_state, state_recreator):
        global state
        UserInterface.UserInterface.__init__(self, proxy_state.bayes,
                                             parm_ini_map, display)
        state = proxy_state
        self.state_recreator = state_recreator # ugly

    def onHome(self):
        """Serve up the homepage."""
        stateDict = state.__dict__.copy()
        stateDict.update(state.bayes.__dict__)
        statusTable = self.html.statusTable.clone()
        if not state.servers:
            statusTable.proxyDetails = "No POP3 proxies running."
        content = (self._buildBox('Status and Configuration',
                                  'status.gif', statusTable % stateDict)+
                   self._buildBox('Train on proxied messages',
                                  'train.gif', self.html.reviewText) +
                   self._buildTrainBox() +
                   self._buildClassifyBox() +
                   self._buildBox('Word query', 'query.gif',
                                  self.html.wordQuery) +
                   self._buildBox('Find message', 'query.gif',
                                  self.html.findMessage)
                   )
        self._writePreamble("Home")
        self.write(content)
        self._writePostamble()

    def onUpload(self, file):
        """Save a message for later training - used by Skip's proxytee.py."""
        # Convert platform-specific line endings into unix-style.
        file = file.replace('\r\n', '\n').replace('\r', '\n')

        # Get a message list from the upload and write it into the cache.
        messages = self._convertUploadToMessageList(file)
        for m in messages:
            messageName = state.getNewMessageName()
            message = state.unknownCorpus.makeMessage(messageName)
            message.setSubstance(m)
            state.unknownCorpus.addMessage(message)

        # Return a link Home.
        self.write("<p>OK. Return <a href='home'>Home</a>.</p>")

    def _keyToTimestamp(self, key):
        """Given a message key (as seen in a Corpus), returns the timestamp
        for that message.  This is the time that the message was received,
        not the Date header."""
        return long(key[:10])

    def _getTimeRange(self, timestamp):
        """Given a unix timestamp, returns a 3-tuple: the start timestamp
        of the given day, the end timestamp of the given day, and the
        formatted date of the given day."""
        # This probably works on Summertime-shift days; time will tell.  8-)
        this = time.localtime(timestamp)
        start = (this[0], this[1], this[2], 0, 0, 0, this[6], this[7], this[8])
        end = time.localtime(time.mktime(start) + 36*60*60)
        end = (end[0], end[1], end[2], 0, 0, 0, end[6], end[7], end[8])
        date = time.strftime("%A, %B %d, %Y", start)
        return time.mktime(start), time.mktime(end), date

    def _buildReviewKeys(self, timestamp):
        """Builds an ordered list of untrained message keys, ready for output
        in the Review list.  Returns a 5-tuple: the keys, the formatted date
        for the list (eg. "Friday, November 15, 2002"), the start of the prior
        page or zero if there isn't one, likewise the start of the given page,
        and likewise the start of the next page."""
        # Fetch all the message keys and sort them into timestamp order.
        allKeys = state.unknownCorpus.keys()
        allKeys.sort()

        # The default start timestamp is derived from the most recent message,
        # or the system time if there are no messages (not that it gets used).
        if not timestamp:
            if allKeys:
                timestamp = self._keyToTimestamp(allKeys[-1])
            else:
                timestamp = time.time()
        start, end, date = self._getTimeRange(timestamp)

        # Find the subset of the keys within this range.
        startKeyIndex = bisect.bisect(allKeys, "%d" % long(start))
        endKeyIndex = bisect.bisect(allKeys, "%d" % long(end))
        keys = allKeys[startKeyIndex:endKeyIndex]
        keys.reverse()

        # What timestamps to use for the prior and next days?  If there any
        # messages before/after this day's range, use the timestamps of those
        # messages - this will skip empty days.
        prior = end = 0
        if startKeyIndex != 0:
            prior = self._keyToTimestamp(allKeys[startKeyIndex-1])
        if endKeyIndex != len(allKeys):
            end = self._keyToTimestamp(allKeys[endKeyIndex])

        # Return the keys and their date.
        return keys, date, prior, start, end

    def _appendMessages(self, table, keyedMessageInfo, label):
        """Appends the rows of a table of messages to 'table'."""
        stripe = 0
        for key, messageInfo in keyedMessageInfo:
            row = self.html.reviewRow.clone()
            if label == 'Spam':
                row.spam.checked = 1
            elif label == 'Ham':
                row.ham.checked = 1
            else:
                row.defer.checked = 1
            row.subject = messageInfo.subjectHeader
            row.subject.title = messageInfo.bodySummary
            row.subject.href="view?key=%s&corpus=%s" % (key, label)
            row.from_ = messageInfo.fromHeader
            subj = cgi.escape(messageInfo.subjectHeader)
            row.classify.href="showclues?key=%s&subject=%s" % (key, subj)
            setattr(row, 'class', ['stripe_on', 'stripe_off'][stripe]) # Grr!
            row = str(row).replace('TYPE', label).replace('KEY', key)
            table += row
            stripe = stripe ^ 1

    def onReview(self, **params):
        """Present a list of message for (re)training."""
        # Train/discard sumbitted messages.
        self._writePreamble("Review")
        id = ''
        numTrained = 0
        numDeferred = 0
        for key, value in params.items():
            if key.startswith('classify:'):
                id = key.split(':')[2]
                if value == 'spam':
                    targetCorpus = state.spamCorpus
                elif value == 'ham':
                    targetCorpus = state.hamCorpus
                elif value == 'discard':
                    targetCorpus = None
                    try:
                        state.unknownCorpus.removeMessage(state.unknownCorpus[id])
                    except KeyError:
                        pass  # Must be a reload.
                else: # defer
                    targetCorpus = None
                    numDeferred += 1
                if targetCorpus:
                    sourceCorpus = None
                    if state.unknownCorpus.get(id) is not None:
                        sourceCorpus = state.unknownCorpus
                    elif state.hamCorpus.get(id) is not None:
                        sourceCorpus = state.hamCorpus
                    elif state.spamCorpus.get(id) is not None:
                        sourceCorpus = state.spamCorpus
                    if sourceCorpus is not None:
                        try:
                            targetCorpus.takeMessage(id, sourceCorpus)
                            if numTrained == 0:
                                self.write("<p><b>Training... ")
                                self.flush()
                            numTrained += 1
                        except KeyError:
                            pass  # Must be a reload.

        # Report on any training, and save the database if there was any.
        if numTrained > 0:
            plural = ''
            if numTrained != 1:
                plural = 's'
            self.write("Trained on %d message%s. " % (numTrained, plural))
            self._doSave()
            self.write("<br>&nbsp;")

        title = ""
        keys = []
        sourceCorpus = state.unknownCorpus
        # If any messages were deferred, show the same page again.
        if numDeferred > 0:
            start = self._keyToTimestamp(id)

        # Else after submitting a whole page, display the prior page or the
        # next one.  Derive the day of the submitted page from the ID of the
        # last processed message.
        elif id:
            start = self._keyToTimestamp(id)
            unused, unused, prior, unused, next = self._buildReviewKeys(start)
            if prior:
                start = prior
            else:
                start = next

        # Else if they've hit Previous or Next, display that page.
        elif params.get('go') == 'Next day':
            start = self._keyToTimestamp(params['next'])
        elif params.get('go') == 'Previous day':
            start = self._keyToTimestamp(params['prior'])

        # Else if an id has been specified, just show that message
        elif params.get('find') is not None:
            key = params['find']
            error = False
            if key == "":
                error = True
                page = "<p>You must enter an id to find.</p>"
            elif state.unknownCorpus.get(key) == None:
                # maybe this message has been moved to the spam
                # or ham corpus
                if state.hamCorpus.get(key) != None:
                    sourceCorpus = state.hamCorpus
                elif state.spamCorpus.get(key) != None:
                    sourceCorpus = state.spamCorpus
                else:
                    error = True
                    page = "<p>Could not find message with id '"
                    page += key + "' - maybe it expired.</p>"
            if error == True:
                title = "Did not find message"
                box = self._buildBox(title, 'status.gif', page)
                self.write(box)
                self.write(self._buildBox('Find message', 'query.gif',
                                          self.html.findMessage))
                self._writePostamble()
                return
            keys.append(params['find'])
            prior = this = next = 0
            title = "Found message"

        # Else show the most recent day's page, as decided by _buildReviewKeys.
        else:
            start = 0

        # Build the lists of messages: spams, hams and unsure.
        if len(keys) == 0:
            keys, date, prior, this, next = self._buildReviewKeys(start)
        keyedMessageInfo = {options.header_spam_string: [],
                            options.header_ham_string: [],
                            options.header_unsure_string: []}
        for key in keys:
            # Parse the message, get the judgement header and build a message
            # info object for each message.
            cachedMessage = sourceCorpus[key]
            message = mboxutils.get_message(cachedMessage.getSubstance())
            judgement = message[options.hammie_header_name]
            if judgement is None:
                judgement = options.header_unsure_string
            else:
                judgement = judgement.split(';')[0].strip()
            messageInfo = self._makeMessageInfo(message)
            keyedMessageInfo[judgement].append((key, messageInfo))

        # Present the list of messages in their groups in reverse order of
        # appearance.
        if keys:
            page = self.html.reviewtable.clone()
            if prior:
                page.prior.value = prior
                del page.priorButton.disabled
            if next:
                page.next.value = next
                del page.nextButton.disabled
            templateRow = page.reviewRow.clone()
            page.table = ""  # To make way for the real rows.
            for header, label in ((options.header_spam_string, 'Spam'),
                                  (options.header_ham_string, 'Ham'),
                                  (options.header_unsure_string, 'Unsure')):
                messages = keyedMessageInfo[header]
                if messages:
                    subHeader = str(self.html.reviewSubHeader)
                    subHeader = subHeader.replace('TYPE', label)
                    page.table += self.html.blankRow
                    page.table += subHeader
                    self._appendMessages(page.table, messages, label)

            page.table += self.html.trainRow
            if title == "":
                title = "Untrained messages received on %s" % date
            box = self._buildBox(title, None, page)  # No icon, to save space.
        else:
            page = "<p>There are no untrained messages to display. "
            page += "Return <a href='home'>Home</a>.</p>"
            title = "No untrained messages"
            box = self._buildBox(title, 'status.gif', page)

        self.write(box)
        self._writePostamble()

    def onView(self, key, corpus):
        """View a message - linked from the Review page."""
        self._writePreamble("View message", parent=('review', 'Review'))
        message = state.unknownCorpus.get(key)
        if message:
            self.write("<pre>%s</pre>" % cgi.escape(message.getSubstance()))
        else:
            self.write("<p>Can't find message %r. Maybe it expired.</p>" % key)
        self._writePostamble()

    def onShowclues(self, key, subject):
        """Show clues for a message - linked from the Review page."""
        self._writePreamble("Message clues", parent=('review', 'Review'))
        message = state.unknownCorpus.get(key).getSubstance()
        message = message.replace('\r\n', '\n').replace('\r', '\n') # For Macs
        if message:
            results = self._buildCluesTable(message, subject)
            del results.classifyAnother
            self.write(results)
        else:
            self.write("<p>Can't find message %r. Maybe it expired.</p>" % key)
        self._writePostamble()

    def _makeMessageInfo(self, message):
        """Given an email.Message, return an object with subjectHeader,
        fromHeader and bodySummary attributes.  These objects are passed into
        appendMessages by onReview - passing email.Message objects directly
        uses too much memory."""
        subjectHeader = message["Subject"] or "(none)"
        fromHeader = message["From"] or "(none)"
        try:
            part = typed_subpart_iterator(message, 'text', 'plain').next()
            text = part.get_payload()
        except StopIteration:
            try:
                part = typed_subpart_iterator(message, 'text', 'html').next()
                text = part.get_payload()
                text, unused = tokenizer.crack_html_style(text)
                text, unused = tokenizer.crack_html_comment(text)
                text = tokenizer.html_re.sub(' ', text)
                text = '(this message only has an HTML body)\n' + text
            except StopIteration:
                text = '(this message has no text body)'
        if type(text) == type([]):  # gotta be a 'right' way to do this
            text = "(this message is a digest of %s messages)" % (len(text))
        else:
            text = text.replace('&nbsp;', ' ')      # Else they'll be quoted
            text = re.sub(r'(\s)\s+', r'\1', text)  # Eg. multiple blank lines
            text = text.strip()

        class _MessageInfo:
            pass
        messageInfo = _MessageInfo()
        messageInfo.subjectHeader = self._trimHeader(subjectHeader, 50, True)
        messageInfo.fromHeader = self._trimHeader(fromHeader, 40, True)
        messageInfo.bodySummary = self._trimHeader(text, 200)
        return messageInfo

    def reReadOptions(self):
        """Called by the config page when the user saves some new options, or
        restores the defaults."""
        # Reload the options.
        global state
        state.bayes.store()
        import Options
        reload(Options)
        global options
        from Options import options

        # Recreate the state.
        self.state_recreator()