# Build the trace-studio workload for QNX 8.0 (source qnxsdp-env.sh first).
ARCH ?= x86_64
CC = qcc -Vgcc_nto$(ARCH)
CFLAGS = -O2 -g -Wall -Wextra -Wno-unused-parameter -Wno-unused-function -std=gnu11
BINS = ts_back ts_front ts_clients ts_psrv ts_pload ts_cancel
OUT = build/$(ARCH)

all: $(addprefix $(OUT)/,$(BINS))

$(OUT)/%: %.c common.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -rf build

.PHONY: all clean
