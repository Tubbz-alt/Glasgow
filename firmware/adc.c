#include <fx2regs.h>
#include <fx2i2c.h>
#include "glasgow.h"

enum {
  // ADC registers
  ADC081_REG_CONV_RESULT   = 0x0,
  ADC081_REG_ALERT_STATUS  = 0x1,
  ADC081_REG_CONFIGURATION = 0x2,
  ADC081_REG_LOW_LIMIT     = 0x3,
  ADC081_REG_HIGH_LIMIT    = 0x4,
  ADC081_REG_HYSTERESIS    = 0x5,
  ADC081_REG_LOWEST_CONV   = 0x6,
  ADC081_REG_HIGHEST_CONV  = 0x7,
  // ADC Conversion Result register bits
  ADC081_BIT_ALERT_FLAG    = 1<<15,
  // ADC Alert Status register bits
  ADC081_BIT_UNDER_RANGE   = 1<<0,
  ADC081_BIT_OVER_RANGE    = 1<<1,
  // ADC Configuration register bits
  ADC081_BIT_POLARITY      = 1<<0,
  ADC081_BIT_ALERT_PIN_EN  = 1<<2,
  ADC081_BIT_ALERT_FLAG_EN = 1<<3,
  ADC081_BIT_ALERT_HOLD    = 1<<4,
};

struct buffer_desc {
  uint8_t selector;
  uint8_t address;
};

static const struct buffer_desc buffers[] = {
  { IO_BUF_A, I2C_ADDR_IOA_ADC },
  { IO_BUF_B, I2C_ADDR_IOB_ADC },
  { 0, 0 }
};

void iobuf_init_adc() {
  // Set up a level-triggered interrupt on INT0# pin.
  PORTACFG |= _INT0;
  TCON &= ~_IT0;
}

static uint16_t code_bytes_to_millivolts(__pdata const uint8_t *code_bytes) {
  // 0x000 = 0 mV, 0xff0 = 6600 mV, 16 LSB = 25.9 mV;
  // highest code word achievable is 0xd50 = 5500 mV,
  // so the following doesn't overflow.
  uint16_t code_word = (((uint16_t)code_bytes[0]) << 8) | code_bytes[1];
  uint16_t millivolts = (code_word >> 4) * 259 / 10;
  return millivolts;
}

static void millivolts_to_code_bytes(uint16_t millivolts, __pdata uint8_t *code_bytes) {
  // See explanation above.
  uint16_t code_word = (millivolts * 10 / 259) << 4;
  code_bytes[0] = code_word >> 8;
  code_bytes[1] = code_word & 0xff;
}

static bool adc_reg_read(uint8_t addr, uint8_t reg,
                         __pdata uint8_t *value, uint8_t length) {
  if(!i2c_start(addr<<1))
    goto fail;
  if(!i2c_write(&reg, 1))
    goto fail;
  if(!i2c_start((addr<<1)|1))
    goto fail;
  if(!i2c_read(value, length))
    goto fail;
  return true;

fail:
  i2c_stop();
  return false;
}

static bool adc_reg_write(uint8_t addr, uint8_t reg,
                          __pdata const uint8_t *value, uint8_t length) {
  if(!i2c_start(addr<<1))
    goto fail;
  if(!i2c_write(&reg, 1))
    goto fail;
  if(!i2c_write(value, length))
    goto fail;
  if(!i2c_stop())
    return false;
  return true;

fail:
  i2c_stop();
  return false;
}

bool iobuf_measure_voltage(uint8_t selector, __xdata uint16_t *millivolts) {
  __code const struct buffer_desc *buffer;
  for(buffer = buffers; buffer->selector; buffer++) {
    if(selector == buffer->selector) {
      __pdata uint8_t code_bytes[2];
      if(!adc_reg_read(buffer->address, ADC081_REG_CONV_RESULT, code_bytes, 2))
        return false;

      *millivolts = code_bytes_to_millivolts(code_bytes);
      return true;
    }
  }

  return false;
}

bool iobuf_set_alert(uint8_t mask,
                     __xdata const uint16_t *low_millivolts,
                     __xdata const uint16_t *high_millivolts) {
  __code const struct buffer_desc *buffer;
  __pdata uint8_t low_code_bytes[2] = { 0x00, 0x00 };
  __pdata uint8_t high_code_bytes[2] = { 0x0f, 0xf0 };
  __pdata uint8_t status_byte = ADC081_BIT_UNDER_RANGE|ADC081_BIT_OVER_RANGE;
  __pdata uint8_t control_byte = 0;

  if(*low_millivolts > MAX_VOLTAGE || *high_millivolts > MAX_VOLTAGE)
    return false;

  if(!(*low_millivolts == 0 && *high_millivolts == MAX_VOLTAGE)) {
    // Alert enabled
    millivolts_to_code_bytes(*low_millivolts, low_code_bytes);
    millivolts_to_code_bytes(*high_millivolts, high_code_bytes);
    control_byte  = ADC081_BIT_ALERT_PIN_EN|ADC081_BIT_ALERT_HOLD;
    control_byte |= 0b110 << 5; // 1 ksps
  }

  for(buffer = buffers; buffer->selector; buffer++) {
    if(mask & buffer->selector) {
      if(!adc_reg_write(buffer->address, ADC081_REG_LOW_LIMIT, low_code_bytes, 2))
        return false;

      if(!adc_reg_write(buffer->address, ADC081_REG_HIGH_LIMIT, high_code_bytes, 2))
        return false;

      if(!adc_reg_write(buffer->address, ADC081_REG_ALERT_STATUS, &status_byte, 1))
        return false;

      if(!adc_reg_write(buffer->address, ADC081_REG_CONFIGURATION, &control_byte, 1))
        return false;
    }
  }

  return true;
}

bool iobuf_get_alert(uint8_t selector,
                     __xdata uint16_t *low_millivolts,
                     __xdata uint16_t *high_millivolts) {
  __code const struct buffer_desc *buffer;
  for(buffer = buffers; buffer->selector; buffer++) {
    if(selector == buffer->selector) {
      __pdata uint8_t code_bytes[2];
      __pdata uint8_t control_byte;

      if(!adc_reg_read(buffer->address, ADC081_REG_CONFIGURATION, &control_byte, 1))
        return false;

      if(control_byte == 0) {
        *low_millivolts = 0;
        *high_millivolts = MAX_VOLTAGE;
        return true;
      }

      if(!adc_reg_read(buffer->address, ADC081_REG_LOW_LIMIT, code_bytes, 2))
        return false;
      *low_millivolts = code_bytes_to_millivolts(code_bytes);

      if(!adc_reg_read(buffer->address, ADC081_REG_HIGH_LIMIT, code_bytes, 2))
        return false;
      *high_millivolts = code_bytes_to_millivolts(code_bytes);

      return true;
    }
  }

  return false;
}

bool iobuf_is_alerted() {
  return !(IOA & (1<<PINA_ALERT_N));
}

bool iobuf_poll_alert(__xdata uint8_t *mask, bool clear) {
  __code const struct buffer_desc *buffer;
  for(*mask = 0, buffer = buffers; buffer->selector; buffer++) {
    __pdata uint8_t status_byte;
    if(!adc_reg_read(buffer->address, ADC081_REG_ALERT_STATUS, &status_byte, 1))
      return false;

    if(status_byte) {
      __pdata uint8_t control_byte = 0;
      *mask |= buffer->selector;

      if(!adc_reg_read(buffer->address, ADC081_REG_CONFIGURATION, &control_byte, 1))
        return false;

      if(clear) {
        // Clear actual alert and re-arm the alert pin
        if(!adc_reg_write(buffer->address, ADC081_REG_ALERT_STATUS, &status_byte, 1))
          return false;
        control_byte |=  ADC081_BIT_ALERT_PIN_EN;
      } else {
        // Only disarm the alert pin (so that alerts from other ADCs can be detected)
        control_byte &= ~ADC081_BIT_ALERT_PIN_EN;
      }

      if(!adc_reg_write(buffer->address, ADC081_REG_CONFIGURATION, &control_byte, 1))
        return false;
    }
  }

  return true;
}
