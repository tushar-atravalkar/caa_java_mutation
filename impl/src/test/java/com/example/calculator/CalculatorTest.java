package com.example.calculator;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CalculatorTest {

    private Calculator calculator;

    @BeforeEach
    void setUp() {
        calculator = new Calculator();
    }

    @Test
    void testAdd() {
        assertEquals(5, calculator.add(2, 3));
    }

    @Test
    void testSubtract() {
        assertEquals(2, calculator.subtract(5, 3));
    }

    @Test
    void testMultiply() {
        assertEquals(6, calculator.multiply(2, 3));
    }

    @Test
    void testDivide() {
        assertEquals(2.0, calculator.divide(6, 3), 1e-9);
    }

    @Test
    void testDivideByZeroThrows() {
        ArithmeticException ex = assertThrows(
                ArithmeticException.class,
                () -> calculator.divide(10, 0)
        );
        assertEquals("Division by zero is not allowed", ex.getMessage());
    }

    @Test
    void testModulo() {
        assertEquals(1, calculator.modulo(7, 3));
    }

    @Test
    void testModuloByZeroThrows() {
        ArithmeticException ex = assertThrows(
                ArithmeticException.class,
                () -> calculator.modulo(10, 0)
        );
        assertEquals("Modulo by zero is not allowed", ex.getMessage());
    }

    @Test
    void testPowerPositiveExponent() {
        assertEquals(8, calculator.power(2, 3));
    }

    @Test
    void testPowerZeroExponentReturnsOne() {
        assertEquals(1, calculator.power(5, 0));
    }

    @Test
    void testPowerNegativeExponentThrows() {
        IllegalArgumentException ex = assertThrows(
                IllegalArgumentException.class,
                () -> calculator.power(2, -1)
        );
        assertEquals("Exponent must not be negative", ex.getMessage());
    }

    @Test
    void absolute_negativeInput_returnsPositiveValue() {
        assertEquals(5, calculator.absolute(-5));
    }

    @Test
    void absolute_positiveInput_returnsSameValue() {
        assertEquals(5, calculator.absolute(5));
    }

    @Test
    void max_firstArgGreater_returnsFirstArg() {
        assertEquals(5, calculator.max(5, 3));
    }

    @Test
    void max_secondArgGreater_returnsSecondArg() {
        assertEquals(5, calculator.max(3, 5));
    }

    @Test
    void min_firstArgSmaller_returnsFirstArg() {
        assertEquals(3, calculator.min(3, 5));
    }

    @Test
    void min_secondArgSmaller_returnsSecondArg() {
        assertEquals(3, calculator.min(5, 3));
    }

    @Test
    void gcd_bothArgsNegative_returnsAbsoluteGcd() {
        assertEquals(4, calculator.gcd(-12, -8));
    }

    @Test
    void gcd_secondArgNegative_returnsAbsoluteGcd() {
        assertEquals(4, calculator.gcd(8, -12));
    }

    @Test
    void gcd_positiveInputsRequiringMultipleIterations_returnsGreatestCommonDivisor() {
        assertEquals(6, calculator.gcd(48, 18));
    }

    @Test
    void lcm_positiveInputs_returnsLeastCommonMultiple() {
        assertEquals(12, calculator.lcm(4, 6));
    }

    @Test
    void lcm_firstArgZero_returnsZero() {
        assertEquals(0, calculator.lcm(0, 5));
    }

    @Test
    void lcm_secondArgZero_returnsZero() {
        assertEquals(0, calculator.lcm(5, 0));
    }

    @Test
    void lcm_negativeInputs_returnsPositiveLeastCommonMultiple() {
        assertEquals(12, calculator.lcm(-4, 6));
        assertEquals(12, calculator.lcm(4, -6));
        assertEquals(12, calculator.lcm(-4, -6));
    }

    @Test
    void square_positiveInput_returnsMultipliedValueNotDivisionOrZero() {
        assertEquals(16, calculator.square(4));
    }

    @Test
    void squareRoot_zeroInput_returnsZeroWithoutThrowing() {
        assertEquals(0.0, calculator.squareRoot(0), 1e-9);
    }

    @Test
    void squareRoot_negativeInput_throwsIllegalArgumentException() {
        IllegalArgumentException ex = assertThrows(
                IllegalArgumentException.class,
                () -> calculator.squareRoot(-4)
        );
        assertEquals("Cannot compute square root of a negative number", ex.getMessage());
    }

    @Test
    void squareRoot_positiveInput_returnsCorrectSquareRootValue() {
        assertEquals(4.0, calculator.squareRoot(16), 1e-9);
    }

    @Test
    void isEven_evenInput_returnsTrue() {
        assertTrue(calculator.isEven(4));
    }

    @Test
    void isEven_zeroInput_returnsTrue() {
        assertTrue(calculator.isEven(0));
    }

    @Test
    void isEven_oddInput_returnsFalse() {
        assertFalse(calculator.isEven(3));
    }

    @Test
    void isEven_negativeOddInput_returnsFalse() {
        assertFalse(calculator.isEven(-3));
    }

    @Test
    void isEven_negativeEvenInput_returnsTrue() {
        assertTrue(calculator.isEven(-4));
    }
}
