#pragma once
#include <stdint.h>

#ifdef BOARD_GUAMNA
/* Moto G Play 2021: 720x1600. Base from the live FDT; stride still unconfirmed */
#define FB_BASE		0x5c000000UL	/* reserved-memory cont_splash_region */
#define FB_W		720
#define FB_H		1600
#define FB_STRIDE	(FB_W * 4)
#else
#define FB_BASE		0x9c000000UL	/* reserved-memory cont_splash_region */
#define FB_W		1080
#define FB_H		2400
#define FB_STRIDE	(FB_W * 4)	/* a8r8g8b8, matches mainline simplefb */
#endif

#define RED	0xffff0000U
#define BLUE	0xff0000a0U
#define WHITE	0xffffffffU
#define YELLOW	0xffffff00U
#define GREEN	0xff00ff00U

extern volatile uint32_t *con_fb;
/* Runtime geometry: defaults are FB_W/FB_H/FB_STRIDE, 4 bytes per pixel; a
 * board may re-point them at the real scanout before the first con_clear(). */
extern uint32_t con_w, con_h, con_stride, con_bpp;

void con_clear(uint32_t bg);
void con_skip(void);
void con_str(const char *label, const char *s);
void con_hex(const char *label, uint64_t v);
/* label, then "a=xxxxxxxx b=xxxxxxxx" style pairs of 32-bit values */
void con_hex2(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb);
