"""
This is the place where we try and discover information buried in images.
"""

from __future__ import division

import sys
import os
import tempfile
import math
import time
import md5
import atexit
try:
    import cPickle as pickle
except ImportError:
    import pickle
try:
    import cStringIO as StringIO
except ImportError:
    import StringIO

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    # We have three possibilities for Set:
    #  (a) With Python 2.2 and earlier, we use our compatsets class
    #  (b) With Python 2.3, we use the sets.Set class
    #  (c) With Python 2.4 and later, we use the builtin set class
    Set = set
except NameError:
    try:
        from sets import Set
    except ImportError:
        from spambayes.compatsets import Set

from spambayes.Options import options

# copied from tokenizer.py - maybe we should split it into pieces...
def log2(n, log=math.log, c=math.log(2)):
    return log(n)/c

# I'm sure this is all wrong for Windows.  Someone else can fix it. ;-)
def is_executable(prog):
    info = os.stat(prog)
    return (info.st_uid == os.getuid() and (info.st_mode & 0100) or
            info.st_gid == os.getgid() and (info.st_mode & 0010) or
            info.st_mode & 0001)

def find_program(prog):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        program = os.path.join(directory, prog)
        if os.path.exists(program) and is_executable(program):
            return program
    return ""

def find_decoders():
    # check for filters to convert to netpbm
    for decode_jpeg in ["jpegtopnm", "djpeg"]:
        if find_program(decode_jpeg):
            break
    else:
        decode_jpeg = None
    for decode_png in ["pngtopnm"]:
        if find_program(decode_png):
            break
    else:
        decode_png = None
    for decode_gif in ["giftopnm"]:
        if find_program(decode_gif):
            break
    else:
        decode_gif = None

    decoders = {
        "image/jpeg": decode_jpeg,
        "image/gif": decode_gif,
        "image/png": decode_png,
        }
    return decoders

def imconcatlr(left, right):
    """Concatenate two images left to right."""
    w1, h1 = left.size
    w2, h2 = right.size
    result = Image.new("RGB", (w1 + w2, max(h1, h2)))
    result.paste(left, (0, 0))
    result.paste(right, (w1, 0))
    return result

def imconcattb(upper, lower):
    """Concatenate two images top to bottom."""
    w1, h1 = upper.size
    w2, h2 = lower.size
    result = Image.new("RGB", (max(w1, w2), h1 + h2))
    result.paste(upper, (0, 0))
    result.paste(lower, (0, h1))
    return result

def pnmsize(pnmfile):
    """Return dimensions of a PNM file."""
    f = open(pnmfile)
    line1 = f.readline()
    line2 = f.readline()
    w, h = [int(n) for n in line2.split()]
    return w, h

def NetPBM_decode_parts(parts, decoders):
    """Decode and assemble a bunch of images using NetPBM tools."""
    rows = []
    tokens = Set()
    for part in parts:
        decoder = decoders.get(part.get_content_type())
        if decoder is None:
            continue
        try:
            bytes = part.get_payload(decode=True)
        except:
            tokens.add("invalid-image:%s" % part.get_content_type())
            continue

        if len(bytes) > options["Tokenizer", "max_image_size"]:
            tokens.add("image:big")
            continue                # assume it's just a picture for now

        fd, imgfile = tempfile.mkstemp()
        os.write(fd, bytes)
        os.close(fd)

        fd, pnmfile = tempfile.mkstemp()
        os.close(fd)
        os.system("%s <%s >%s 2>dev.null" % (decoder, imgfile, pnmfile))
        w, h = pnmsize(pnmfile)
        if not rows:
            # first image
            rows.append([pnmfile])
        elif pnmsize(rows[-1][-1])[1] != h:
            # new image, different height => start new row
            rows.append([pnmfile])
        else:
            # new image, same height => extend current row
            rows[-1].append(pnmfile)

    for (i, row) in enumerate(rows):
        if len(row) > 1:
            fd, pnmfile = tempfile.mkstemp()
            os.close(fd)
            os.system("pnmcat -lr %s > %s 2>/dev/null" %
                      (" ".join(row), pnmfile))
            for f in row:
                os.unlink(f)
            rows[i] = pnmfile
        else:
            rows[i] = row[0]

    fd, pnmfile = tempfile.mkstemp()
    os.close(fd)
    os.system("pnmcat -tb %s > %s 2>/dev/null" % (" ".join(rows), pnmfile))
    for f in rows:
        os.unlink(f)
    return [pnmfile], tokens

def PIL_decode_parts(parts):
    """Decode and assemble a bunch of images using PIL."""
    tokens = Set()
    rows = []
    for part in parts:
        try:
            bytes = part.get_payload(decode=True)
        except:
            tokens.add("invalid-image:%s" % part.get_content_type())
            continue

        if len(bytes) > options["Tokenizer", "max_image_size"]:
            tokens.add("image:big")
            continue                # assume it's just a picture for now

        # We're dealing with spammers and virus writers here.  Who knows
        # what garbage they will call a GIF image to entice you to open
        # it?
        try:
            image = Image.open(StringIO.StringIO(bytes))
            image.load()
        except IOError:
            tokens.add("invalid-image:%s" % part.get_content_type())
            continue
        else:
            image = image.convert("RGB")

        if not rows:
            # first image
            rows.append(image)
        elif image.size[1] != rows[-1].size[1]:
            # new image, different height => start new row
            rows.append(image)
        else:
            # new image, same height => extend current row
            rows[-1] = imconcatlr(rows[-1], image)

    if not rows:
        return [], tokens

    # now concatenate the resulting row images top-to-bottom
    full_image, rows = rows[0], rows[1:]
    for image in rows:
        full_image = imconcattb(full_image, image)

    fd, pnmfile = tempfile.mkstemp()
    os.close(fd)
    full_image.save(open(pnmfile, "wb"), "PPM")

    return [pnmfile], tokens

class ImageStripper:
    def __init__(self, cachefile=""):
        self.cachefile = os.path.expanduser(cachefile)
        if os.path.exists(self.cachefile):
            self.cache = pickle.load(open(self.cachefile))
        else:
            self.cache = {}
        self.misses = self.hits = 0
        if self.cachefile:
            atexit.register(self.close)

    def extract_ocr_info(self, pnmfiles):
        fd, orf = tempfile.mkstemp()
        os.close(fd)

        textbits = []
        tokens = Set()
        scale = options["Tokenizer", "ocrad_scale"] or 1
        charset = options["Tokenizer", "ocrad_charset"]
        for pnmfile in pnmfiles:
            fhash = md5.new(open(pnmfile).read()).hexdigest()
            if fhash in self.cache:
                self.hits += 1
                ctext, ctokens = self.cache[fhash]
            else:
                self.misses += 1
                ocr = os.popen("ocrad -s %s -c %s -x %s < %s 2>/dev/null" %
                               (scale, charset, orf, pnmfile))
                ctext = ocr.read().lower()
                ocr.close()
                ctokens = set()
                for line in open(orf):
                    if line.startswith("lines"):
                        nlines = int(line.split()[1])
                        if nlines:
                            ctokens.add("image-text-lines:%d" %
                                        int(log2(nlines)))
                self.cache[fhash] = (ctext, ctokens)
            textbits.append(ctext)
            tokens |= ctokens
            os.unlink(pnmfile)
        os.unlink(orf)

        return "\n".join(textbits), tokens

    def analyze(self, parts):
        if not parts:
            return "", Set()

        # need ocrad
        if not find_program("ocrad"):
            return "", Set()

        if Image is not None:
            pnmfiles, tokens = PIL_decode_parts(parts)
        else:
            if not find_program("pnmcat"):
                return "", Set()
            pnmfiles, tokens = NetPBM_decode_parts(parts, find_decoders())

        if pnmfiles:
            text, new_tokens = self.extract_ocr_info(pnmfiles)
            return text, tokens | new_tokens

        return "", tokens


    def close(self):
        if options["globals", "verbose"]:
            print >> sys.stderr, "saving", len(self.cache),
            print >> sys.stderr, "items to", self.cachefile,
            if self.hits + self.misses:
                print >> sys.stderr, "%.2f%% hit rate" % \
                      (100 * self.hits / (self.hits + self.misses)),
            print >> sys.stderr
        pickle.dump(self.cache, open(self.cachefile, "wb"))

_cachefile = options["Tokenizer", "crack_image_cache"]
crack_images = ImageStripper(_cachefile).analyze
