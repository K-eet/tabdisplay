PKGS := libpipewire-0.3 libavcodec libavfilter libavutil libdrm
CFLAGS ?= -O2 -g -Wall -Wextra
CFLAGS += $(shell pkg-config --cflags $(PKGS))
LDLIBS += $(shell pkg-config --libs $(PKGS)) -lpthread

tdcap: tdcap.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f tdcap

.PHONY: clean
