#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/pio.h"
#include "hardware/clocks.h"
#include "hardware/irq.h"
#include "hardware/structs/systick.h"
#include "hardware/regs/pio.h"
#include "tusb.h"
#include "uart9.pio.h"
#define MAXCMD 512
#define DI 0
#define DE 1
#define RE 2
#define RO 3
#define LED 25
#define TXSM 0
#define RXSM 1
static PIO p=pio0;
typedef struct {uint8_t len, expected;uint16_t xor_expected,w[7];} Cmd;
typedef struct {uint16_t w[6];uint8_t count,err,confirm,valid;uint16_t xor_actual,delay;} Rs;
typedef struct {uint16_t n;uint8_t auto_mode,retries;uint32_t start,gap,period,ret_start,confirm_delay;} Settings;
_Static_assert(sizeof(Cmd)==18,"Cmd");_Static_assert(sizeof(Rs)==20,"Rs");_Static_assert(sizeof(Settings)==24,"Settings");
static Cmd cmds[MAXCMD];static Rs rs[MAXCMD][6];static Settings s={0,0,0,8800,1584,42240,8800,0};
static uint8_t state;static uint16_t completed;static uint txoff,rxoff;static uint32_t last_tick;static uint64_t tick_sum;
static uint64_t ticks(void){uint32_t v=systick_hw->cvr&0xffffff;tick_sum+=(last_tick-v)&0xffffff;last_tick=v;return tick_sum;}
static void until(uint64_t t){while(ticks()<t)tight_loop_contents();}
static void idle_bus(void){gpio_put(DE,0);gpio_put(RE,0);}
static void rx_reset(void){pio_sm_set_enabled(p,RXSM,false);pio_sm_clear_fifos(p,RXSM);pio_sm_restart(p,RXSM);pio_sm_exec(p,RXSM,pio_encode_jmp(rxoff));pio_interrupt_clear(p,0);}
static void line_init(void){
 gpio_init(DE);gpio_init(RE);gpio_set_dir(DE,true);gpio_set_dir(RE,true);idle_bus();
 txoff=pio_add_program(p,&tx9_program);rxoff=pio_add_program(p,&rx9_program);
 pio_gpio_init(p,DI);pio_gpio_init(p,RO);gpio_pull_up(RO);
 pio_sm_config a=tx9_program_get_default_config(txoff);sm_config_set_out_pins(&a,DI,1);sm_config_set_out_shift(&a,true,true,11);sm_config_set_fifo_join(&a,PIO_FIFO_JOIN_TX);sm_config_set_clkdiv(&a,10.0f);pio_sm_init(p,TXSM,txoff,&a);pio_sm_set_consecutive_pindirs(p,TXSM,DI,1,true);pio_sm_set_pins_with_mask(p,TXSM,1u<<DI,1u<<DI);
 pio_sm_config b=rx9_program_get_default_config(rxoff);sm_config_set_in_pins(&b,RO);sm_config_set_in_shift(&b,true,false,32);sm_config_set_fifo_join(&b,PIO_FIFO_JOIN_RX);sm_config_set_clkdiv(&b,10.0f);pio_sm_init(p,RXSM,rxoff,&b);pio_sm_set_consecutive_pindirs(p,RXSM,RO,1,false);
}
/* Returns software-observed end of last stop bit. RX armed before receiver enable. */
static uint64_t tx_started;
static uint64_t send_words(const uint16_t *w,uint8_t n,bool listen){
 rx_reset();gpio_put(RE,1);pio_sm_set_enabled(p,TXSM,false);pio_sm_clear_fifos(p,TXSM);pio_sm_restart(p,TXSM);pio_interrupt_clear(p,1);
 p->sm[TXSM].shiftctrl &= ~PIO_SM0_SHIFTCTRL_AUTOPULL_BITS;
 pio_sm_put(p,TXSM,11u*n-1u);pio_sm_exec(p,TXSM,pio_encode_pull(false,true));pio_sm_exec(p,TXSM,pio_encode_mov(pio_x,pio_osr));
 for(uint8_t i=0;i<n;i++)pio_sm_put(p,TXSM,((uint32_t)(w[i]&511)<<1)|1024u);
 pio_sm_exec(p,TXSM,pio_encode_pull(false,true));p->sm[TXSM].shiftctrl |= PIO_SM0_SHIFTCTRL_AUTOPULL_BITS;pio_sm_exec(p,TXSM,pio_encode_jmp(txoff));gpio_put(DE,1);until(ticks()+80);tx_started=ticks();pio_sm_set_enabled(p,TXSM,true);
 while(!pio_interrupt_get(p,1))ticks();uint64_t end=ticks();pio_sm_set_enabled(p,TXSM,false);gpio_put(DE,0);gpio_put(RE,0);if(listen)pio_sm_set_enabled(p,RXSM,true);return end;
}
static void receive(Rs *r,uint64_t end,uint32_t start_window){
 r->valid=1;uint64_t deadline=end+start_window;
 while(r->count<255){
  while(!pio_interrupt_get(p,0)){if(ticks()>=deadline)goto done;}
  uint64_t start=ticks();pio_interrupt_clear(p,0);if(!r->count){uint64_t d=start-end;r->delay=d>65535?65535:(uint16_t)d;}
  /* A started word must finish, even when it crosses the start deadline. */
  while(pio_sm_is_rx_fifo_empty(p,RXSM)){if(ticks()-start>4000)goto done;}
  uint32_t raw=pio_sm_get(p,RXSM)>>22;uint16_t word=raw&511;uint8_t i=r->count++;
  if(i<6){r->w[i]=word;r->xor_actual^=word;if(!(raw&512))r->err|=1u<<i;}
  /* PUSH occurs near the end of the stop bit; software timestamp has small bias. */
  deadline=ticks()+s.gap;
 }
done: pio_sm_set_enabled(p,RXSM,false);
}
static uint8_t decision(const Cmd *c,const Rs *r,unsigned attempt){
 if(!s.auto_mode||r->count==255||(!attempt&&!r->count))return 0;
 if(r->err)return 3;
 if(!r->count||(r->count==1&&r->w[0]==0xaa)||r->count<c->expected)return attempt<s.retries?2:3;
 return r->xor_actual==c->xor_expected?1:3;
}
static void run(void){memset(rs,0,sizeof rs);completed=0;state=2;gpio_put(LED,1);uint64_t next=ticks();
 for(unsigned i=0;i<s.n;i++){
  until(next);irq_set_enabled(USBCTRL_IRQ,false);uint64_t end=send_words(cmds[i].w,cmds[i].len,true);next=tx_started+s.period;
  for(unsigned j=0;j<6;j++){Rs *r=&rs[i][j];receive(r,end,j?s.ret_start:s.start);r->confirm=decision(&cmds[i],r,j);if(!r->confirm)break;until(ticks()+s.confirm_delay);uint16_t w=r->confirm==1?0:r->confirm==2?0xaa:0xff;end=send_words(&w,1,r->confirm==2);if(r->confirm!=2)break;}
  idle_bus();completed++;irq_set_enabled(USBCTRL_IRQ,true);tud_task();ticks();
 }
 gpio_put(LED,0);state=3;
}
/* USB serialization, CRC-32/ISO-HDLC */
static uint32_t crc_update(uint32_t c,const void *data,size_t n){const uint8_t *a=data;while(n--){c^=*a++;for(int k=0;k<8;k++)c=(c>>1)^((0u-(c&1))&0xedb88320u);}return c;}
static uint32_t get32(const uint8_t *b){return (uint32_t)b[0]|(uint32_t)b[1]<<8|(uint32_t)b[2]<<16|(uint32_t)b[3]<<24;}
static void put32(uint8_t *b,uint32_t v){for(int i=0;i<4;i++)b[i]=v>>(8*i);}
static bool usb_write(const void *v,size_t n){const uint8_t *b=v;uint64_t deadline=ticks()+1200000000ull;while(n){tud_task();if(ticks()>deadline)return false;if(!tud_cdc_connected())return false;uint32_t k=tud_cdc_write(b,n);if(k){b+=k;n-=k;tud_cdc_write_flush();deadline=ticks()+1200000000ull;}}return true;}
static uint32_t out_crc;static bool out_ok;
static void out_begin(uint8_t op,uint32_t n){uint8_t h[7]={0xa5,0x5a,op};put32(h+3,n);out_crc=crc_update(~0u,h+2,5);out_ok=usb_write(h,7);}
static void out_data(const void *b,size_t n){out_crc=crc_update(out_crc,b,n);if(out_ok)out_ok=usb_write(b,n);}
static void out_end(void){uint8_t b[4];put32(b,~out_crc);if(out_ok)out_ok=usb_write(b,4);}
static void message(uint8_t op,const void *b,size_t n){out_begin(op,n);out_data(b,n);out_end();}
static void ack(uint8_t op){message(0x81,&op,1);}
static void fail(uint8_t op,uint8_t e){uint8_t b[]={op,e};message(0xff,b,2);}
static void results(void){uint8_t h[]={0,0,completed&255,completed>>8};out_begin(0x82,4+138u*completed);out_data(h,4);for(unsigned i=0;i<completed&&out_ok;i++){out_data(&cmds[i],18);out_data(rs[i],120);}out_end();if(!out_ok)state=4;}
static bool settings_ok(const Settings *x){return x->n>=1&&x->n<=512&&x->auto_mode<=1&&x->retries<=5&&x->start>=1&&x->start<=65535&&x->gap>=1&&x->gap<=65535&&x->period>=38720&&x->ret_start>=1&&x->ret_start<=65535&&x->confirm_delay<=65535;}
static uint8_t payload[9240];
static void handle(uint8_t op,uint32_t n){
 if(op==1){state=0;if(n<24){fail(op,2);return;}Settings x;memcpy(&x,payload,24);if(!settings_ok(&x)){fail(op,4);return;}if(n!=24+18u*x.n){fail(op,2);return;}
 for(unsigned i=0;i<x.n;i++){Cmd c;memcpy(&c,payload+24+18*i,18);if(c.len<1||c.len>7||c.expected<1||c.expected>6||c.xor_expected>511){fail(op,4);return;}for(unsigned j=0;j<7;j++)if(c.w[j]>511||(j>=c.len&&c.w[j])){fail(op,4);return;}cmds[i]=c;}
 s=x;memset(rs,0,sizeof rs);rx_reset();completed=0;state=1;ack(op);return;}
 if(op<2||op>6){fail(op,1);return;}if(n){fail(op,2);return;}
 if(op==2||op==3){if(state!=(op==2?1:3)){fail(op,5);return;}ack(op);if(!out_ok){state=4;return;}run();results();return;}
 if(op==4){idle_bus();state=5;ack(op);}else if(op==5)message(0x85,&s,24);else message(0x86,&state,1);
}
/* Byte stream parser; bounded payload, CRC validation before execution. */
static void parse(uint8_t b){static uint8_t phase,op,idx,h[4];static uint32_t n,pos,c;static uint64_t last;uint64_t now=ticks();if(phase&&now-last>1200000000ull)phase=0;last=now;
 switch(phase){case 0:if(b==0xa5)phase=1;break;case 1:if(b==0x5a)phase=2;else phase=b==0xa5?1:0;break;case 2:op=b;if(op==1)state=0;c=crc_update(~0u,&b,1);idx=0;phase=3;break;case 3:h[idx++]=b;c=crc_update(c,&b,1);if(idx==4){n=get32(h);pos=0;idx=0;if(n>sizeof payload){phase=0;fail(op,2);}else phase=n?4:5;}break;case 4:payload[pos++]=b;c=crc_update(c,&b,1);if(pos==n){idx=0;phase=5;}break;case 5:h[idx++]=b;if(idx==4){phase=0;if(get32(h)!=~c)fail(op,3);else handle(op,n);}break;}
}
int main(void){set_sys_clock_khz(120000,true);systick_hw->rvr=0xffffff;systick_hw->cvr=0;systick_hw->csr=5;last_tick=systick_hw->cvr&0xffffff;gpio_init(LED);gpio_set_dir(LED,true);line_init();tusb_init();while(true){tud_task();ticks();if(tud_cdc_available()){uint8_t b;if(tud_cdc_read(&b,1)==1)parse(b);}}}
