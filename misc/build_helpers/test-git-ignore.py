﻿#!/usr/bin/env python
# -*- coding: utf-8-with-signature-unix; fill-column: 77 -*-

import sys
from subprocess import Popen, PIPE

cmd = ["git", "status", "--porcelain"]
p = Popen(cmd, stdout=PIPE)
output = p.communicate()[0]
print output
if output == "":
    sys.exit(0)
sys.exit(1)


